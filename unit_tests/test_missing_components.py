"""
Unit tests for the six missing components + end-to-end integration test.

Components tested:
  1. Unchoking / preferred-neighbor selection + download rate tracking
  2. HAVE message handling
  3. REQUEST message handling
  4. UNCHOKE triggers piece request
  5. Global termination condition
  6. Logger integration across all events
  E2E. Full two-peer file transfer over real TCP
"""

import math
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ── make the project root importable ──
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import protocol
import state
from file_manager import FileManager
from logger import PeerLogger
from peer_network import PeerNetwork


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeConn:
    """Lightweight stand-in for connection.Connection used in unit tests."""

    def __init__(self, name="fake"):
        self.name = name
        self.sent: list[bytes] = []
        self.closed = False

    def send(self, data: bytes):
        self.sent.append(data)

    def close(self):
        self.closed = True


def _make_peernetwork(**overrides):
    """Construct a PeerNetwork without starting it (no sockets)."""
    defaults = dict(
        my_peer_id=1001,
        peers=[],
        on_bytes_received=lambda conn, msg: None,
    )
    defaults.update(overrides)
    return PeerNetwork(**defaults)


def _parse_messages(raw_list: list[bytes]):
    """Feed a list of raw wire bytes through a MessageFramer and return parsed msgs."""
    framer = protocol.MessageFramer()
    framer.handshake_done = True
    msgs = []
    for raw in raw_list:
        framer.feed(raw)
        while True:
            m = framer.next_message()
            if m is None:
                break
            msgs.append(m)
    return msgs


# ===================================================================
#  1. Unchoking / preferred-neighbor selection & download rate tracking
# ===================================================================

class TestUnchokingAndRateTracking(unittest.TestCase):
    """Missing component #1: periodic preferred-neighbor selection
    based on download rates, and optimistic unchoking."""

    def test_download_rate_recording_and_reset(self):
        ps = state.PeerState(8, has_file=False)
        ps.add_neighbor(2001)
        ps.add_neighbor(2002)

        ps.record_download(2001, 100)
        ps.record_download(2001, 50)
        ps.record_download(2002, 200)

        rates = ps.get_and_reset_download_rates()
        self.assertEqual(rates[2001], 150)
        self.assertEqual(rates[2002], 200)

        # After reset, rates should be empty
        rates2 = ps.get_and_reset_download_rates()
        self.assertEqual(rates2, {})

    def test_select_preferred_neighbors_by_rate(self):
        """Top-k should be selected by download rate among interested peers."""
        pn = _make_peernetwork()
        pn.common = {"NumberOfPreferredNeighbors": 2}
        pn.peer_state = state.PeerState(8, has_file=True)  # has file → serves pieces

        # Three neighbors, all interested
        for pid in [2001, 2002, 2003]:
            pn.peer_state.add_neighbor(pid)
            pn.peer_state.set_peer_interested(pid, True)

        # Simulate rates: 2002 fastest, 2003 second, 2001 slowest
        pn.peer_state.record_download(2001, 10)
        pn.peer_state.record_download(2002, 300)
        pn.peer_state.record_download(2003, 150)

        # Create fake connections
        conns = {pid: FakeConn(name=str(pid)) for pid in [2001, 2002, 2003]}
        pn.connections = conns

        pn.logger = MagicMock()

        # Peer does NOT have complete file → selection by rate
        pn.peer_state.my_bitfield = state.Bitfield(8, has_file=False)
        pn._select_preferred_neighbors()

        # top-2 should be 2002, 2003
        self.assertIn(2002, pn._preferred_ids)
        self.assertIn(2003, pn._preferred_ids)
        self.assertNotIn(2001, pn._preferred_ids)

        # Logger should have been called
        pn.logger.log_preferred_neighbors.assert_called_once()

    def test_select_preferred_neighbors_random_when_complete(self):
        """When the local peer has the complete file, selection is random among interested."""
        pn = _make_peernetwork()
        pn.common = {"NumberOfPreferredNeighbors": 1}
        pn.peer_state = state.PeerState(4, has_file=True)

        for pid in [2001, 2002]:
            pn.peer_state.add_neighbor(pid)
            pn.peer_state.set_peer_interested(pid, True)

        conns = {pid: FakeConn() for pid in [2001, 2002]}
        pn.connections = conns
        pn.logger = MagicMock()

        # Run selection many times; both peers should appear eventually (random)
        seen = set()
        for _ in range(50):
            pn._select_preferred_neighbors()
            seen.update(pn._preferred_ids)
        self.assertEqual(seen, {2001, 2002})

    def test_optimistic_unchoke_selects_choked_interested(self):
        """Optimistic unchoking should only pick from choked + interested neighbours."""
        pn = _make_peernetwork()
        pn.common = {}
        pn.peer_state = state.PeerState(8, has_file=True)

        # 2001: choked, interested → candidate
        pn.peer_state.add_neighbor(2001)
        pn.peer_state.set_peer_interested(2001, True)
        # am_choking defaults to True ✓

        # 2002: unchoked, interested → NOT candidate
        pn.peer_state.add_neighbor(2002)
        pn.peer_state.set_peer_interested(2002, True)
        pn.peer_state.set_am_choking(2002, False)

        # 2003: choked, NOT interested → NOT candidate
        pn.peer_state.add_neighbor(2003)

        conns = {pid: FakeConn() for pid in [2001, 2002, 2003]}
        pn.connections = conns
        pn._preferred_ids = {2002}
        pn.logger = MagicMock()

        pn._select_optimistic_neighbor()
        self.assertEqual(pn._optimistic_id, 2001)
        pn.logger.log_optimistic_neighbor.assert_called_with(2001)

    def test_unchoke_sends_unchoke_message(self):
        """_select_preferred_neighbors must send UNCHOKE to newly-preferred neighbours."""
        pn = _make_peernetwork()
        pn.common = {"NumberOfPreferredNeighbors": 1}
        pn.peer_state = state.PeerState(4, has_file=True)

        pn.peer_state.add_neighbor(2001)
        pn.peer_state.set_peer_interested(2001, True)

        conn = FakeConn()
        pn.connections = {2001: conn}
        pn.logger = MagicMock()

        pn._select_preferred_neighbors()

        msgs = _parse_messages(conn.sent)
        types = [m[1][0] for m in msgs if m[0] == "message"]
        self.assertIn(protocol.MsgType.UNCHOKE, types)

    def test_choke_non_preferred_neighbor(self):
        """Non-preferred neighbours that were previously unchoked should receive CHOKE."""
        pn = _make_peernetwork()
        pn.common = {"NumberOfPreferredNeighbors": 1}
        # Use has_file=False so selection is by rate, not random
        pn.peer_state = state.PeerState(4, has_file=False)

        # Two interested neighbors
        for pid in [2001, 2002]:
            pn.peer_state.add_neighbor(pid)
            pn.peer_state.set_peer_interested(pid, True)

        # 2002 was previously unchoked
        pn.peer_state.set_am_choking(2002, False)

        conns = {pid: FakeConn() for pid in [2001, 2002]}
        pn.connections = conns
        pn.logger = MagicMock()

        # Simulate higher rate for 2001 so it gets selected
        pn.peer_state.record_download(2001, 1000)
        pn.peer_state.record_download(2002, 1)

        pn._select_preferred_neighbors()

        # 2002 should have been choked
        msgs_2002 = _parse_messages(conns[2002].sent)
        types_2002 = [m[1][0] for m in msgs_2002 if m[0] == "message"]
        self.assertIn(protocol.MsgType.CHOKE, types_2002)


# ===================================================================
#  2. HAVE message handling
# ===================================================================

class TestHaveMessageHandling(unittest.TestCase):
    """Missing component #2: when we receive HAVE, we must update
    the neighbour's bitfield, log it, and re-evaluate interest."""

    def test_have_updates_bitfield_and_triggers_interest(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(8, has_file=False)
        pn.peer_state.add_neighbor(2001)
        pn.logger = MagicMock()
        pn.file_manager = MagicMock()

        conn = FakeConn()
        pn.connections = {2001: conn}
        pn.framers = {2001: protocol.MessageFramer()}
        pn.framers[2001].handshake_done = True

        # Simulate receiving a HAVE message for piece 3
        have_msg = protocol.create_message(protocol.MsgType.HAVE, struct.pack(">I", 3))
        pn.on_bytes_handler(conn, have_msg)

        # Neighbour should now have piece 3
        self.assertTrue(pn.peer_state.neighbor_has_piece(2001, 3))

        # Logger should have recorded it
        pn.logger.log_received_have.assert_called_with(2001, 3, True)

        # Since we don't have piece 3, we should become interested
        # → an INTERESTED message should have been sent
        msgs = _parse_messages(conn.sent)
        types = [m[1][0] for m in msgs if m[0] == "message"]
        self.assertIn(protocol.MsgType.INTERESTED, types)

    def test_have_for_already_owned_piece_no_interest(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(8, has_file=False)
        pn.peer_state.add_neighbor(2001)
        pn.peer_state.mark_piece_downloaded(3)  # we already have piece 3
        pn.logger = MagicMock()
        pn.file_manager = MagicMock()

        conn = FakeConn()
        pn.connections = {2001: conn}
        pn.framers = {2001: protocol.MessageFramer()}
        pn.framers[2001].handshake_done = True

        have_msg = protocol.create_message(protocol.MsgType.HAVE, struct.pack(">I", 3))
        pn.on_bytes_handler(conn, have_msg)

        # Peer 2001 has piece 3 which we also have → NOT INTERESTED (or no interest msg)
        msgs = _parse_messages(conn.sent)
        types = [m[1][0] for m in msgs if m[0] == "message"]
        # Either no message, or NOT_INTERESTED
        self.assertNotIn(protocol.MsgType.INTERESTED, types)


# ===================================================================
#  3. REQUEST message handling
# ===================================================================

class TestRequestMessageHandling(unittest.TestCase):
    """Missing component #3: when we receive REQUEST, we serve the piece
    if the requester is unchoked."""

    def test_request_from_unchoked_peer_sends_piece(self):
        with tempfile.TemporaryDirectory() as td:
            pn = _make_peernetwork()
            pn.peer_state = state.PeerState(4, has_file=True)
            pn.peer_state.add_neighbor(2001)
            pn.peer_state.set_am_choking(2001, False)  # unchoked

            pn.file_manager = FileManager(
                peer_id=1001, file_name="t.dat",
                file_size=8, piece_size=2, base_dir=td,
            )
            # Write the piece we'll serve
            pn.file_manager.write_piece(1, b"AB")

            pn.logger = MagicMock()

            conn = FakeConn()
            pn.connections = {2001: conn}
            pn.framers = {2001: protocol.MessageFramer()}
            pn.framers[2001].handshake_done = True

            req_msg = protocol.create_message(protocol.MsgType.REQUEST, struct.pack(">I", 1))
            pn.on_bytes_handler(conn, req_msg)

            # Should have sent a PIECE message back
            msgs = _parse_messages(conn.sent)
            piece_msgs = [m for m in msgs if m[0] == "message" and m[1][0] == protocol.MsgType.PIECE]
            self.assertEqual(len(piece_msgs), 1)
            payload = piece_msgs[0][1][1]
            idx = struct.unpack(">I", payload[:4])[0]
            data = payload[4:]
            self.assertEqual(idx, 1)
            self.assertEqual(data, b"AB")

    def test_request_from_choked_peer_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            pn = _make_peernetwork()
            pn.peer_state = state.PeerState(4, has_file=True)
            pn.peer_state.add_neighbor(2001)
            # am_choking defaults to True → peer is choked

            pn.file_manager = FileManager(
                peer_id=1001, file_name="t.dat",
                file_size=8, piece_size=2, base_dir=td,
            )
            pn.file_manager.write_piece(1, b"AB")

            pn.logger = MagicMock()

            conn = FakeConn()
            pn.connections = {2001: conn}
            pn.framers = {2001: protocol.MessageFramer()}
            pn.framers[2001].handshake_done = True

            req_msg = protocol.create_message(protocol.MsgType.REQUEST, struct.pack(">I", 1))
            pn.on_bytes_handler(conn, req_msg)

            # Should NOT have sent a PIECE message
            msgs = _parse_messages(conn.sent)
            piece_msgs = [m for m in msgs if m[0] == "message" and m[1][0] == protocol.MsgType.PIECE]
            self.assertEqual(len(piece_msgs), 0)


# ===================================================================
#  4. UNCHOKE triggers piece request
# ===================================================================

class TestUnchokeTriggersRequest(unittest.TestCase):
    """Missing component #4: receiving UNCHOKE should immediately
    trigger a REQUEST for a missing piece."""

    def test_unchoke_triggers_request(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(8, has_file=False)
        pn.peer_state.add_neighbor(2001)
        pn.peer_state.neighbor_set_piece(2001, 5)
        pn.peer_state.set_peer_choking(2001, True)
        pn.logger = MagicMock()
        pn.file_manager = MagicMock()

        conn = FakeConn()
        pn.connections = {2001: conn}
        pn.framers = {2001: protocol.MessageFramer()}
        pn.framers[2001].handshake_done = True

        unchoke_msg = protocol.create_message(protocol.MsgType.UNCHOKE)
        pn.on_bytes_handler(conn, unchoke_msg)

        # Logger should record unchoke
        pn.logger.log_unchoked_by.assert_called_with(2001)

        # Should have sent a REQUEST
        msgs = _parse_messages(conn.sent)
        req_msgs = [m for m in msgs if m[0] == "message" and m[1][0] == protocol.MsgType.REQUEST]
        self.assertEqual(len(req_msgs), 1)
        piece_idx = struct.unpack(">I", req_msgs[0][1][1])[0]
        self.assertEqual(piece_idx, 5)

    def test_choke_clears_outstanding_request(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(8, has_file=False)
        pn.peer_state.add_neighbor(2001)
        pn.peer_state.neighbor_set_piece(2001, 5)
        pn.peer_state.set_peer_choking(2001, False)
        pn.peer_state.reserve_request(2001, 5)
        pn.logger = MagicMock()
        pn.file_manager = MagicMock()

        conn = FakeConn()
        pn.connections = {2001: conn}
        pn.framers = {2001: protocol.MessageFramer()}
        pn.framers[2001].handshake_done = True

        choke_msg = protocol.create_message(protocol.MsgType.CHOKE)
        pn.on_bytes_handler(conn, choke_msg)

        # Outstanding request should be cleared
        self.assertIsNone(pn.peer_state.get_outstanding_request(2001))
        pn.logger.log_choked_by.assert_called_with(2001)


# ===================================================================
#  5. Global termination condition
# ===================================================================

class TestTermination(unittest.TestCase):
    """Missing component #5: peer shuts down once every peer
    (self + all neighbours) has the complete file."""

    def test_all_done_set_when_all_complete(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(4, has_file=True)  # we have everything
        pn._completion_logged = True  # file already assembled
        pn.peer_state.add_neighbor(2001)
        # Mark neighbour as complete
        for i in range(4):
            pn.peer_state.neighbor_set_piece(2001, i)

        pn._check_termination()
        self.assertTrue(pn.all_done.is_set())

    def test_all_done_not_set_when_neighbor_incomplete(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(4, has_file=True)
        pn.peer_state.add_neighbor(2001)
        pn.peer_state.neighbor_set_piece(2001, 0)  # only 1 of 4

        pn._check_termination()
        self.assertFalse(pn.all_done.is_set())

    def test_all_done_not_set_when_self_incomplete(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(4, has_file=False)
        pn.peer_state.add_neighbor(2001)
        for i in range(4):
            pn.peer_state.neighbor_set_piece(2001, i)

        pn._check_termination()
        self.assertFalse(pn.all_done.is_set())

    def test_termination_triggered_by_have(self):
        """Receiving the final HAVE from the last neighbor should set all_done."""
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(2, has_file=True)
        pn._completion_logged = True  # file already assembled
        pn.peer_state.add_neighbor(2001)
        pn.peer_state.neighbor_set_piece(2001, 0)
        pn.logger = MagicMock()
        pn.file_manager = MagicMock()

        conn = FakeConn()
        pn.connections = {2001: conn}
        pn.framers = {2001: protocol.MessageFramer()}
        pn.framers[2001].handshake_done = True

        # Send HAVE for the final piece
        have_msg = protocol.create_message(protocol.MsgType.HAVE, struct.pack(">I", 1))
        pn.on_bytes_handler(conn, have_msg)

        self.assertTrue(pn.all_done.is_set())


# ===================================================================
#  6. Logger integration
# ===================================================================

class TestLoggerIntegration(unittest.TestCase):
    """Missing component #6: logger methods must be called at
    every required event."""

    def _setup_pn(self):
        pn = _make_peernetwork()
        pn.peer_state = state.PeerState(8, has_file=False)
        pn.peer_state.add_neighbor(2001)
        pn.logger = MagicMock()
        pn.file_manager = MagicMock()

        conn = FakeConn()
        pn.connections = {2001: conn}
        pn.framers = {2001: protocol.MessageFramer()}
        pn.framers[2001].handshake_done = True
        return pn, conn

    def test_log_interested(self):
        pn, conn = self._setup_pn()
        msg = protocol.create_message(protocol.MsgType.INTERESTED)
        pn.on_bytes_handler(conn, msg)
        # Verify called with peer_id=2001 and a list of wanted pieces
        call_args = pn.logger.log_received_interested.call_args
        self.assertEqual(call_args[0][0], 2001)       # remote_id
        self.assertIsInstance(call_args[0][1], list)  # wanted_pieces

    def test_log_not_interested(self):
        pn, conn = self._setup_pn()
        msg = protocol.create_message(protocol.MsgType.NOT_INTERESTED)
        pn.on_bytes_handler(conn, msg)
        # Verify called with peer_id=2001 and an int piece count
        call_args = pn.logger.log_received_not_interested.call_args
        self.assertEqual(call_args[0][0], 2001)         # remote_id
        self.assertIsInstance(call_args[0][1], int)     # remote_piece_count

    def test_log_choked_by(self):
        pn, conn = self._setup_pn()
        msg = protocol.create_message(protocol.MsgType.CHOKE)
        pn.on_bytes_handler(conn, msg)
        pn.logger.log_choked_by.assert_called_with(2001)

    def test_log_unchoked_by(self):
        pn, conn = self._setup_pn()
        msg = protocol.create_message(protocol.MsgType.UNCHOKE)
        pn.on_bytes_handler(conn, msg)
        pn.logger.log_unchoked_by.assert_called_with(2001)

    def test_log_received_have(self):
        pn, conn = self._setup_pn()
        msg = protocol.create_message(protocol.MsgType.HAVE, struct.pack(">I", 7))
        pn.on_bytes_handler(conn, msg)
        # Verify called with peer_id=2001, piece_index=7, and a bool for 'needed'
        call_args = pn.logger.log_received_have.call_args
        self.assertEqual(call_args[0][0], 2001)  # remote_id
        self.assertEqual(call_args[0][1], 7)      # piece_index
        self.assertIsInstance(call_args[0][2], bool)  # needed

    def test_log_connection_made_format(self):
        """Verify actual log output format matches project spec."""
        with tempfile.TemporaryDirectory() as td:
            logger = PeerLogger(1001, base_dir=td)
            logger.log_connection_made(1002)
            logger.close()

            text = (Path(td) / "log_peer_1001.log").read_text()
            self.assertIn("Peer 1001 makes a connection to Peer 1002.", text)

    def test_log_connected_from_format(self):
        with tempfile.TemporaryDirectory() as td:
            logger = PeerLogger(1001, base_dir=td)
            logger.log_connected_from(1002)
            logger.close()

            text = (Path(td) / "log_peer_1001.log").read_text()
            self.assertIn("Peer 1001 is connected from Peer 1002.", text)

    def test_log_preferred_neighbors_format(self):
        with tempfile.TemporaryDirectory() as td:
            logger = PeerLogger(1001, base_dir=td)
            logger.log_preferred_neighbors([1002, 1003])
            logger.close()

            text = (Path(td) / "log_peer_1001.log").read_text()
            self.assertIn("Peer 1001 has the preferred neighbors 1002,1003.", text)

    def test_log_optimistic_neighbor_format(self):
        with tempfile.TemporaryDirectory() as td:
            logger = PeerLogger(1001, base_dir=td)
            logger.log_optimistic_neighbor(1003)
            logger.close()

            text = (Path(td) / "log_peer_1001.log").read_text()
            self.assertIn(
                "Peer 1001 has the optimistically unchoked neighbor 1003.", text
            )

    def test_log_downloaded_piece_format(self):
        with tempfile.TemporaryDirectory() as td:
            logger = PeerLogger(1001, base_dir=td)
            logger.log_downloaded_piece(piece_index=5, remote_id=1002, num_pieces=10)
            logger.close()

            text = (Path(td) / "log_peer_1001.log").read_text()
            self.assertIn(
                "Peer 1001 has downloaded the piece 5 from Peer 1002. "
                "Now the number of pieces it has is 10.", text
            )


# ===================================================================
#  E2E. End-to-end: two-peer file transfer over real TCP
# ===================================================================

class TestEndToEndTransfer(unittest.TestCase):
    """
    Spin up two peers (seeder + leecher) in-process using real TCP.
    The seeder has the complete file, the leecher has nothing.
    After unchoking, REQUEST/PIECE exchanges should cause the leecher
    to download the complete file and both peers to signal termination.
    """

    def test_seeder_leecher_transfer(self):
        td = tempfile.mkdtemp()
        try:
            # ── config on disk ──
            cfg_dir = os.path.join(td, "cfg")
            os.makedirs(cfg_dir)

            file_data = b"ABCDEFGHIJKLMNOP"  # 16 bytes, 4 pieces of 4 bytes
            file_size = len(file_data)
            piece_size = 4
            num_pieces = math.ceil(file_size / piece_size)

            with open(os.path.join(cfg_dir, "Common.cfg"), "w") as f:
                f.write(f"NumberOfPreferredNeighbors 1\n")
                f.write(f"UnchokingInterval 1\n")
                f.write(f"OptimisticUnchokingInterval 2\n")
                f.write(f"FileName test_e2e.dat\n")
                f.write(f"FileSize {file_size}\n")
                f.write(f"PieceSize {piece_size}\n")

            # Pick two free ports
            s1 = socket.socket(); s1.bind(("127.0.0.1", 0)); port1 = s1.getsockname()[1]; s1.close()
            s2 = socket.socket(); s2.bind(("127.0.0.1", 0)); port2 = s2.getsockname()[1]; s2.close()

            with open(os.path.join(cfg_dir, "PeerInfo.cfg"), "w") as f:
                f.write(f"1001 127.0.0.1 {port1} 1\n")
                f.write(f"1002 127.0.0.1 {port2} 0\n")

            # ── prepare seeder file ──
            seeder_dir = os.path.join(td, "peer_1001")
            os.makedirs(seeder_dir, exist_ok=True)
            with open(os.path.join(seeder_dir, "test_e2e.dat"), "wb") as f:
                f.write(file_data)

            # ── change cwd so config_loader finds cfg/ ──
            old_cwd = os.getcwd()
            os.chdir(td)

            try:
                import config_loader
                peers = config_loader.parse_peerinfo_cfg()
                noop = lambda c, m: None

                seeder = PeerNetwork(1001, peers, on_bytes_received=noop, bind_host="127.0.0.1")
                leecher = PeerNetwork(1002, peers, on_bytes_received=noop, bind_host="127.0.0.1")

                seeder.start()
                time.sleep(0.3)
                leecher.start()

                # Wait for completion (both peers have files) – up to 30 s
                done = leecher.all_done.wait(timeout=30)

                # ── assertions ──
                self.assertTrue(done, "Leecher did not complete file download in time")
                self.assertTrue(leecher.peer_state.has_complete_file())

                # Verify actual file bytes on disk
                leecher_file = Path(td) / "peer_1002" / "test_e2e.dat"
                self.assertTrue(leecher_file.exists(), "Assembled file not found")
                self.assertEqual(leecher_file.read_bytes(), file_data)

                # Verify pieces individually
                for i in range(num_pieces):
                    expected_len = min(piece_size, file_size - i * piece_size)
                    expected_data = file_data[i * piece_size : i * piece_size + expected_len]
                    actual = leecher.file_manager.read_piece(i)
                    self.assertEqual(actual, expected_data, f"Piece {i} mismatch")

                # Verify log files exist
                seeder_log = Path(td) / "log_peer_1001.log"
                leecher_log = Path(td) / "log_peer_1002.log"
                self.assertTrue(seeder_log.exists())
                self.assertTrue(leecher_log.exists())

                leecher_text = leecher_log.read_text()
                self.assertIn("has downloaded the complete file", leecher_text)

            finally:
                try:
                    seeder.stop()
                except:
                    pass
                try:
                    leecher.stop()
                except:
                    pass
                os.chdir(old_cwd)
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
