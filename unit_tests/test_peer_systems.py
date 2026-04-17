import struct
import tempfile
import unittest
from pathlib import Path

import protocol
import state
from file_manager import FileManager
from logger import PeerLogger
from peer_network import PeerNetwork


class FakeConn:
    def __init__(self, name="fake"):
        self.name = name
        self.sent = []
        self.closed = False

    def send(self, data: bytes):
        self.sent.append(data)

    def close(self):
        self.closed = True


class TestProtocol(unittest.TestCase):
    def test_handshake_round_trip(self):
        msg = protocol.create_handshake(1234)
        self.assertEqual(len(msg), protocol.HANDSHAKE_LEN)
        framer = protocol.MessageFramer()
        framer.feed(msg)
        kind, peer_id = framer.next_message()
        self.assertEqual(kind, "handshake")
        self.assertEqual(peer_id, 1234)

    def test_message_round_trip(self):
        payload = struct.pack(">I", 7)
        msg = protocol.create_message(protocol.MsgType.HAVE, payload)
        framer = protocol.MessageFramer()
        framer.handshake_done = True
        framer.feed(msg)
        kind, (msg_type, out_payload) = framer.next_message()
        self.assertEqual(kind, "message")
        self.assertEqual(msg_type, protocol.MsgType.HAVE)
        self.assertEqual(out_payload, payload)


class TestBitfieldAndState(unittest.TestCase):
    def test_bitfield_set_has_and_trailing_bits(self):
        bf = state.Bitfield(10, has_file=False)
        bf.set_piece(0)
        bf.set_piece(9)

        self.assertTrue(bf.has_piece(0))
        self.assertTrue(bf.has_piece(9))
        self.assertFalse(bf.has_piece(1))

        raw = bf.to_bytes()
        self.assertEqual(len(raw), 2)

        bf2 = state.Bitfield(10, has_file=False)
        bf2.from_bytes(raw)
        self.assertTrue(bf2.has_piece(0))
        self.assertTrue(bf2.has_piece(9))

    def test_peerstate_select_random_interesting_piece(self):
        ps = state.PeerState(8, has_file=False)
        ps.add_neighbor(2001)
        ps.neighbor_set_piece(2001, 3)
        ps.neighbor_set_piece(2001, 6)

        self.assertIn(ps.select_random_interesting_piece(2001), {3, 6})
        ps.mark_piece_downloaded(3)
        self.assertEqual(ps.select_random_interesting_piece(2001), 6)

    def test_reserve_and_clear_request(self):
        ps = state.PeerState(8, has_file=False)
        ps.add_neighbor(2001)

        self.assertTrue(ps.reserve_request(2001, 4))
        self.assertEqual(ps.get_outstanding_request(2001), 4)
        self.assertFalse(ps.reserve_request(2001, 4))

        ps.clear_outstanding_request(2001)
        self.assertIsNone(ps.get_outstanding_request(2001))


class TestFileManager(unittest.TestCase):
    def test_write_read_and_assemble(self):
        with tempfile.TemporaryDirectory() as td:
            fm = FileManager(
                peer_id=1001,
                file_name="test.dat",
                file_size=10,
                piece_size=4,
                base_dir=td,
            )

            self.assertEqual(fm.num_pieces, 3)
            self.assertEqual(fm.piece_length(0), 4)
            self.assertEqual(fm.piece_length(1), 4)
            self.assertEqual(fm.piece_length(2), 2)

            fm.write_piece(0, b"abcd")
            fm.write_piece(1, b"efgh")
            fm.write_piece(2, b"ij")

            self.assertTrue(fm.has_piece(0))
            self.assertEqual(fm.read_piece(1), b"efgh")

            out = fm.assemble_file()
            self.assertTrue(out.exists())
            self.assertEqual(out.read_bytes(), b"abcdefghij")


class TestLogger(unittest.TestCase):
    def test_logger_writes_file(self):
        with tempfile.TemporaryDirectory() as td:
            logger = PeerLogger(1001, base_dir=td)
            logger.log_connection_made(1002)
            logger.log_received_have(1002, 3, needed=True)
            logger.log_completed_file()
            logger.close()

            path = Path(td) / "log_peer_1001.log"
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("Peer 1001 makes a connection to Peer 1002.", text)
            self.assertIn("received the 'have' message", text)
            self.assertIn("has downloaded the complete file.", text)


class TestPeerNetworkHelpers(unittest.TestCase):
    def test_maybe_request_piece_sends_request(self):
        pn = PeerNetwork(
            my_peer_id=1001,
            peers=[],
            on_bytes_received=lambda conn, msg: None,
        )

        pn.peer_state = state.PeerState(8, has_file=False)
        pn.peer_state.add_neighbor(2001)
        pn.peer_state.neighbor_set_piece(2001, 5)
        pn.peer_state.set_peer_choking(2001, False)

        conn = FakeConn()
        pn._maybe_request_piece(2001, conn)

        self.assertEqual(len(conn.sent), 1)
        framer = protocol.MessageFramer()
        framer.handshake_done = True
        framer.feed(conn.sent[0])
        kind, (msg_type, payload) = framer.next_message()
        self.assertEqual(kind, "message")
        self.assertEqual(msg_type, protocol.MsgType.REQUEST)
        self.assertEqual(struct.unpack(">I", payload)[0], 5)

    def test_handle_piece_received_marks_piece_and_broadcasts_have(self):
        with tempfile.TemporaryDirectory() as td:
            pn = PeerNetwork(
                my_peer_id=1001,
                peers=[],
                on_bytes_received=lambda conn, msg: None,
            )

            pn.peer_state = state.PeerState(4, has_file=False)
            pn.peer_state.add_neighbor(2001)
            pn.peer_state.neighbor_set_piece(2001, 1)
            pn.peer_state.set_peer_choking(2001, False)

            pn.file_manager = FileManager(
                peer_id=1001,
                file_name="test.dat",
                file_size=8,
                piece_size=2,
                base_dir=td,
            )
            pn.logger = PeerLogger(1001, base_dir=td)

            try:
                self.assertTrue(pn.peer_state.reserve_request(2001, 1))

                captured = FakeConn()
                pn.connections = {2001: captured}

                pn._handle_piece_received(2001, 1, b"xy")

                self.assertTrue(pn.peer_state.my_bitfield.has_piece(1))
                self.assertIsNone(pn.peer_state.get_outstanding_request(2001))
                self.assertEqual(len(captured.sent), 1)

                framer = protocol.MessageFramer()
                framer.handshake_done = True
                framer.feed(captured.sent[0])
                kind, (msg_type, payload) = framer.next_message()

                self.assertEqual(kind, "message")
                self.assertEqual(msg_type, protocol.MsgType.HAVE)
                self.assertEqual(struct.unpack(">I", payload)[0], 1)
            finally:
                pn.logger.close()


if __name__ == "__main__":
    unittest.main()