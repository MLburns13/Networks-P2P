import socket
import threading
import time
from typing import List, Callable, Optional, Dict, Set
import config_loader
import connection
import protocol
import state
import random
import struct
from file_manager import FileManager
from logger import PeerLogger

class PeerNetwork:
    def __init__(self, my_peer_id: int, peers: List[config_loader.PeerInfo],
                    on_bytes_received: Callable[[connection.Connection, bytes], None],
                    on_connection_made: Optional[Callable[[connection.Connection, config_loader.PeerInfo], None]] = None,
                    on_connection_received: Optional[Callable[[connection.Connection, config_loader.PeerInfo], None]] = None,
                    bind_host: str = "0.0.0.0"):
        self.my_peer_id = my_peer_id
        self.peers = peers
        self.on_bytes_received = on_bytes_received
        self.on_connection_made = on_connection_made
        self.on_connection_received = on_connection_received
        self.bind_host = bind_host
        self.connections: Dict[int, connection.Connection] = {}
        self.framers: Dict[int, protocol.MessageFramer] = {}
        self.peer_state: Optional[state.PeerState] = None
        self.connections_lock = threading.Lock()
        self.server_sock = None
        self.server_thread = None
        self.running = False
        self.outgoing_retry_interval = 0.5   # initial backoff (doubles each attempt, capped at 5s)
        self.outgoing_retry_attempts = 60     # enough for large networks under load
        
        self.my_index = -1
        self.common = {}
        self.file_manager: Optional[FileManager] = None
        self.logger: Optional[PeerLogger] = None
        self._completion_logged = False

        # Unchoking state
        self._preferred_ids: Set[int] = set()
        self._optimistic_id: Optional[int] = None
        self._unchoking_thread: Optional[threading.Thread] = None
        self._optimistic_thread: Optional[threading.Thread] = None

        # Termination event – set when all peers have the complete file
        self.all_done = threading.Event()

    def start(self):
        my_entry = None
        for idx, p in enumerate(self.peers):
            if p.peer_id == self.my_peer_id:
                my_entry = p
                self.my_index = idx
                break
        if my_entry is None:
            raise ValueError(f"my_peer_id {self.my_peer_id} not found in PeerInfo list")
            
        # Initialize the global PeerState with numPieces from common properties
        self.common = config_loader.parse_common_cfg()
        num_pieces = int(self.common["numPieces"])
        file_name = str(self.common["FileName"])
        file_size = int(self.common["FileSize"])
        piece_size = int(self.common["PieceSize"])

        self.peer_state = state.PeerState(num_pieces, my_entry.has_file)
        self.file_manager = FileManager(
            peer_id=self.my_peer_id,
            file_name=file_name,
            file_size=file_size,
            piece_size=piece_size,
            base_dir=".",
        )
        self.logger = PeerLogger(self.my_peer_id, base_dir=".")

        # If we start with the complete file, mark completion immediately
        if my_entry.has_file:
            self._completion_logged = True

        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.bind_host, my_entry.port))
        # Backlog scaled to network size so the OS queue doesn't fill under burst load
        listen_backlog = max(128, len(self.peers))
        self.server_sock.listen(listen_backlog)

        self.running = True
        self.server_thread = threading.Thread(target=self._accept_loop, daemon=True, name="accept-loop")
        self.server_thread.start()
        print(f"[PeerNetwork] Listening on {self.bind_host}:{my_entry.port} (peer {self.my_peer_id})")

        #connect to all prior peers in the file ordering
        threading.Thread(target=self._connect_to_previous_peers, daemon=True).start()

        # Start unchoking timer threads
        self._unchoking_thread = threading.Thread(target=self._unchoking_timer_loop, daemon=True, name="unchoke-timer")
        self._unchoking_thread.start()

        self._optimistic_thread = threading.Thread(target=self._optimistic_timer_loop, daemon=True, name="opt-unchoke-timer")
        self._optimistic_thread.start()
        
        print(f"\n{'='*60}")
        print(f"[STARTUP] Peer {self.my_peer_id} starting")
        print(f"  FileName              : {file_name}")
        print(f"  FileSize              : {file_size} bytes")
        print(f"  PieceSize             : {piece_size} bytes")
        print(f"  NumPieces             : {num_pieces}")
        print(f"  NumberOfPreferredNeighbors : {self.common.get('NumberOfPreferredNeighbors')}")
        print(f"  UnchokingInterval     : {self.common.get('UnchokingInterval')} s")
        print(f"  OptimisticUnchokingInterval: {self.common.get('OptimisticUnchokingInterval')} s")
        print(f"  Has file at start     : {my_entry.has_file}")
        print(f"  Initial bitfield      : {self.peer_state.my_bitfield.count()}/{num_pieces} pieces")
        print(f"{'='*60}\n")

    # ----------------------------------------------------------------
    # Unchoking timer loops  (Missing component #1)
    # ----------------------------------------------------------------

    def _unchoking_timer_loop(self):
        """Every UnchokingInterval seconds, reselect preferred neighbors."""
        interval = int(self.common.get("UnchokingInterval", 5))
        while self.running:
            time.sleep(interval)
            if not self.running:
                break
            try:
                self._select_preferred_neighbors()
            except Exception as e:
                print(f"[PeerNetwork] unchoking timer error: {e}")

    def _optimistic_timer_loop(self):
        """Every OptimisticUnchokingInterval seconds, pick an optimistic neighbor."""
        interval = int(self.common.get("OptimisticUnchokingInterval", 15))
        while self.running:
            time.sleep(interval)
            if not self.running:
                break
            try:
                self._select_optimistic_neighbor()
            except Exception as e:
                print(f"[PeerNetwork] optimistic timer error: {e}")

    def _select_preferred_neighbors(self):
        """Determine top-k preferred neighbors based on download rate (or random if we have the complete file)."""
        assert self.peer_state is not None
        assert self.logger is not None
        
        print(f"\n[UNCHOKE-TIMER] Peer {self.my_peer_id}: reselecting preferred neighbors (interval={self.common.get('UnchokingInterval')}s) ...")

        k = int(self.common.get("NumberOfPreferredNeighbors", 2))
        rates = self.peer_state.get_and_reset_download_rates()
        interested = self.peer_state.get_interested_neighbors()

        if self.peer_state.has_complete_file():
            # If we have the full file, pick k random interested neighbors
            selected = random.sample(interested, min(k, len(interested)))
        else:
            # Sort by download rate: highest first, break ties randomly
            random.shuffle(interested)  # randomise order before stable sort for tie-breaking
            interested.sort(key=lambda pid: rates.get(pid, 0), reverse=True)
            selected = interested[:k]

        selected_set = set(selected)
        self._preferred_ids = selected_set

        # Log preferred neighbors
        self.logger.log_preferred_neighbors(sorted(selected_set))

        # Unchoke selected, choke everyone else (except optimistic neighbor)
        with self.connections_lock:
            all_pids = [pid for pid in self.connections if pid > 0]

        for pid in all_pids:
            conn = self.get_connection(pid)
            if conn is None:
                continue
            if pid in selected_set:
                if self.peer_state.is_am_choking(pid):
                    self.peer_state.set_am_choking(pid, False)
                    try:
                        conn.send(protocol.create_message(protocol.MsgType.UNCHOKE))
                        print(f"[UNCHOKE-TIMER] Sent UNCHOKE to preferred neighbor {pid}")
                    except Exception as e:
                        print(f"[PeerNetwork] failed to send UNCHOKE to {pid}: {e}")
            else:
                if pid == self._optimistic_id:
                    continue  # don't choke the optimistic neighbor
                if not self.peer_state.is_am_choking(pid):
                    self.peer_state.set_am_choking(pid, True)
                    try:
                        conn.send(protocol.create_message(protocol.MsgType.CHOKE))
                        print(f"[UNCHOKE-TIMER] Sent CHOKE to {pid} (no longer preferred)")
                    except Exception as e:
                        print(f"[PeerNetwork] failed to send CHOKE to {pid}: {e}")
        
        self._restart_stalled_requests()
                        
    def _restart_stalled_requests(self):
        """For every peer we're unchoked by and interested in, ensure there
        is an outstanding request. Recovers from any stalled download loop."""
        assert self.peer_state is not None
        with self.connections_lock:
            peer_ids = [pid for pid in self.connections if pid > 0]

        for pid in peer_ids:
            if (not self.peer_state.is_peer_choking_us(pid)
                    and self.peer_state.is_am_interested_in_peer(pid)
                    and self.peer_state.get_outstanding_request(pid) is None):
                conn = self.get_connection(pid)
                if conn is not None:
                    print(f"[WATCHDOG] Restarting stalled request loop with peer {pid}")
                    self._maybe_request_piece(pid, conn)

    def _select_optimistic_neighbor(self):
        """Pick a random choked-but-interested neighbor to optimistically unchoke."""
        assert self.peer_state is not None
        assert self.logger is not None
        
        print(f"\n[OPT-UNCHOKE-TIMER] Peer {self.my_peer_id}: reselecting optimistic neighbor (interval={self.common.get('OptimisticUnchokingInterval')}s) ...")

        candidates = self.peer_state.get_choked_interested_neighbors(exclude=self._preferred_ids)
        if not candidates:
            print(f"[OPT-UNCHOKE-TIMER] No choked interested neighbors available to unchoke.")
            return

        chosen = random.choice(candidates)

        # If we had a previous optimistic neighbor that isn't a preferred neighbor, re-choke it
        old = self._optimistic_id
        if old is not None and old != chosen and old not in self._preferred_ids:
            if not self.peer_state.is_am_choking(old):
                self.peer_state.set_am_choking(old, True)
                conn = self.get_connection(old)
                if conn is not None:
                    try:
                        conn.send(protocol.create_message(protocol.MsgType.CHOKE))
                        print(f"[OPT-UNCHOKE-TIMER] Sent CHOKE to previous optimistic neighbor {old}")
                    except Exception as e:
                        print(f"[PeerNetwork] failed to choke old optimistic {old}: {e}")

        self._optimistic_id = chosen
        self.peer_state.set_am_choking(chosen, False)
        conn = self.get_connection(chosen)
        if conn is not None:
            try:
                conn.send(protocol.create_message(protocol.MsgType.UNCHOKE))
                print(f"[OPT-UNCHOKE-TIMER] Sent UNCHOKE to optimistic neighbor {chosen}")
            except Exception as e:
                print(f"[PeerNetwork] failed to send UNCHOKE to optimistic {chosen}: {e}")

        self.logger.log_optimistic_neighbor(chosen)

    # ----------------------------------------------------------------
    # Connection accept / outgoing
    # ----------------------------------------------------------------

    def _accept_loop(self):
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                
                # To assign a temporary ID until the handshake completes, we'll use the negative port for now.
                # Once the handshake comes in, the true remote peer ID is established.
                tmp_id = -addr[1] 
                
                conn = connection.Connection(client_sock, addr, on_bytes=self.on_bytes_handler, on_close=self.on_conn_close_handler, name=f"in-{addr}")
                
                with self.connections_lock:
                    self.connections[tmp_id] = conn
                    self.framers[tmp_id] = protocol.MessageFramer()
                    
                # Send our handshake right away before starting recv loop 
                # to prevent race conditions where we reply to their handshake 
                # before we send our own!
                try:
                    conn.send(protocol.create_handshake(self.my_peer_id))
                except Exception as e:
                    print(f"[PeerNetwork] Error sending handshake: {e}")
                    
                conn.start()
                
                if self.on_connection_received:
                    tmp_peer = config_loader.PeerInfo(peer_id=-1, host=addr[0], port=addr[1], has_file=False)
                    try:
                        self.on_connection_received(conn, tmp_peer)
                    except Exception as e:
                        print(f"[PeerNetwork] on_connection_received callback error: {e}")
                        
                print(f"[PeerNetwork] Accepted connection from {addr}")
            except Exception as e:
                if self.running:
                    print(f"[PeerNetwork] accept error: {e}")
                time.sleep(0.1)

    def _connect_to_previous_peers(self):
        #connect to peers that are earlier in the peerinfo ordering (index < my_index)
        for i in range(0, self.my_index):
            peer_to = self.peers[i]
            
            with self.connections_lock:
                if peer_to.peer_id in self.connections:
                    print(f"[PeerNetwork] Already connected to {peer_to.peer_id}, skipping outgoing connection.")
                    continue

            attempt = 0
            connected = False
            
            while attempt < self.outgoing_retry_attempts and not connected and self.running:
                attempt += 1
                try:
                    sock = socket.create_connection((peer_to.host, peer_to.port), timeout=5)
                    sock.settimeout(None)
                    conn = connection.Connection(sock, (peer_to.host, peer_to.port), on_bytes=self.on_bytes_handler, on_close=self.on_conn_close_handler, name=f"out-{peer_to.peer_id}")
                    
                    with self.connections_lock:
                        if peer_to.peer_id in self.connections:
                            print(f"[PeerNetwork] Connection arrived while connecting to {peer_to.peer_id}, aborting outgoing.")
                            conn.close()
                            connected = True # Handled by incoming
                            continue
                            
                        self.connections[peer_to.peer_id] = conn
                        self.framers[peer_to.peer_id] = protocol.MessageFramer()
                    
                    # Send our handshake right away before starting recv loop
                    try:
                        conn.send(protocol.create_handshake(self.my_peer_id))
                    except Exception as e:
                        print(f"[PeerNetwork] Error sending handshake: {e}")
                        
                    conn.start()
                        
                    print(f"[PeerNetwork] Outgoing connection established to {peer_to.peer_id}@{peer_to.host}:{peer_to.port}")

                    # Log the outgoing connection
                    if self.logger:
                        self.logger.log_connection_made(peer_to.peer_id)
                    
                    if self.on_connection_made:
                        try:
                            self.on_connection_made(conn, peer_to)
                        except Exception as e:
                            print(f"[PeerNetwork] on_connection_made callback error: {e}")
                            
                    connected = True
                except Exception as e:
                    print(f"[PeerNetwork] Could not connect to {peer_to.peer_id}@{peer_to.host}:{peer_to.port} (attempt {attempt}): {e}")
                    # Exponential backoff: 0.5s, 1s, 2s, 4s … capped at 5s
                    backoff = min(self.outgoing_retry_interval * (2 ** (attempt - 1)), 5.0)
                    time.sleep(backoff)
                    
            if not connected:
                print(f"[PeerNetwork] FAILED to connect to {peer_to.peer_id} after {self.outgoing_retry_attempts} attempts")

    def _get_conn_peer_id(self, conn: connection.Connection) -> Optional[int]:
        with self.connections_lock:
            for pid, c in self.connections.items():
                if c is conn:
                    return pid
        return None

    def _rekey_connection(self, old_id: int, new_id: int, conn: connection.Connection, framer: protocol.MessageFramer):
        with self.connections_lock:
            if new_id in self.connections and self.connections[new_id] is not conn:
                raise RuntimeError(f"duplicate connection detected for {new_id}")
            self.connections[new_id] = conn
            self.framers[new_id] = framer
            if old_id in self.connections:
                del self.connections[old_id]
            if old_id in self.framers:
                del self.framers[old_id]

    # ----------------------------------------------------------------
    # Interest / request helpers
    # ----------------------------------------------------------------

    def _sync_interest_for_peer(self, remote_id: int, conn: connection.Connection):
        assert self.peer_state is not None

        missing = self.peer_state.get_interesting_pieces(remote_id)
        interested = len(missing) > 0
        current = self.peer_state.is_am_interested_in_peer(remote_id)

        if interested and not current:
            self.peer_state.set_am_interested(remote_id, True)
            try:
                conn.send(protocol.create_message(protocol.MsgType.INTERESTED))
                print(f"[INTERESTED] Peer {self.my_peer_id} sent INTERESTED to {remote_id}")
            except Exception as e:
                print(f"[PeerNetwork] failed to send INTERESTED to {remote_id}: {e}")
        elif not interested and current:
            self.peer_state.set_am_interested(remote_id, False)
            try:
                conn.send(protocol.create_message(protocol.MsgType.NOT_INTERESTED))
                print(f"[NOT-INTERESTED] Peer {self.my_peer_id} sent NOT_INTERESTED to {remote_id}")
            except Exception as e:
                print(f"[PeerNetwork] failed to send NOT_INTERESTED to {remote_id}: {e}")

    def _broadcast_have(self, piece_index: int):
        assert self.peer_state is not None
        with self.connections_lock:
            items = list(self.connections.items())

        for pid, conn in items:
            if pid <= 0:
                continue
            try:
                conn.send(protocol.create_message(protocol.MsgType.HAVE, struct.pack(">I", piece_index)))
                print(f"[HAVE] Peer {self.my_peer_id} broadcasting HAVE(piece={piece_index}) to {pid}")
            except Exception as e:
                print(f"[PeerNetwork] failed to send HAVE({piece_index}) to {pid}: {e}")      

    def _request_next_piece(self, remote_id: int):
        assert self.peer_state is not None

        conn = self.get_connection(remote_id)
        if conn is None:
            return

        if self.peer_state.is_peer_choking_us(remote_id):
            return

        next_piece = self.peer_state.select_random_piece(remote_id)
        if next_piece is None:
            return

        if not self.peer_state.reserve_request(remote_id, next_piece):
            return

        try:
            conn.send(
                protocol.create_message(
                    protocol.MsgType.REQUEST,
                    struct.pack(">I", next_piece),
                )
            )
            print(f"[REQUEST] Peer {self.my_peer_id} sent REQUEST for piece {next_piece} to {remote_id}")
        except Exception as e:
            print(f"[PeerNetwork] failed to send REQUEST({next_piece}) to {remote_id}: {e}")
            self.peer_state.clear_outstanding_request(remote_id)

    def _maybe_request_piece(self, remote_id: int, conn: connection.Connection):
        assert self.peer_state is not None

        if self.peer_state.is_peer_choking_us(remote_id):
            return

        piece_index = self.peer_state.select_random_piece(remote_id)
        if piece_index is None:
            return

        if not self.peer_state.reserve_request(remote_id, piece_index):
            return

        try:
            conn.send(
                protocol.create_message(
                    protocol.MsgType.REQUEST,
                    struct.pack(">I", piece_index),
                )
            )
            print(f"[REQUEST] Peer {self.my_peer_id} sent REQUEST for piece {piece_index} to {remote_id}")
        except Exception as e:
            print(f"[PeerNetwork] failed to send REQUEST({piece_index}) to {remote_id}: {e}")
            self.peer_state.clear_outstanding_request(remote_id)

    def _handle_piece_received(self, remote_id: int, piece_index: int, piece_data: bytes):
        assert self.peer_state is not None
        assert self.file_manager is not None
        assert self.logger is not None

        outstanding = self.peer_state.get_outstanding_request(remote_id)
        if outstanding != piece_index:
            if not self.peer_state.is_peer_choking_us(remote_id):
                self._request_next_piece(remote_id)
            return

        try:
            self.file_manager.write_piece(piece_index, piece_data)
        except Exception as e:
            print(f"[PeerNetwork] failed writing piece {piece_index}: {e}")
            self.peer_state.clear_outstanding_request(remote_id)
            return

        self.peer_state.clear_outstanding_request(remote_id)

        # Track download rate (Missing component #1 – rate tracking)
        self.peer_state.record_download(remote_id, len(piece_data))

        piece_count = self.peer_state.mark_piece_downloaded(piece_index)
        self.logger.log_downloaded_piece(piece_index, remote_id, piece_count)

        self._broadcast_have(piece_index)

        # Re-evaluate interest in all neighbors because this new piece may change it.
        with self.connections_lock:
            peer_ids = [pid for pid in self.connections.keys() if pid > 0]

        for pid in peer_ids:
            conn = self.get_connection(pid)
            if conn is not None:
                self._sync_interest_for_peer(pid, conn)

        # Completion handling
        if self.peer_state.has_complete_file():
            if not self._completion_logged:
                try:
                    self.file_manager.assemble_file()
                except Exception as e:
                    print(f"[PeerNetwork] failed assembling complete file: {e}")
                self.logger.log_completed_file()
                self._completion_logged = True

            # Check global termination (Missing component #5)
            self._check_termination()
            # Don't request more pieces, but keep processing messages
            return

        # Request the next piece from the same peer, if possible.
        self._request_next_piece(remote_id)

    # ----------------------------------------------------------------
    # Global termination check  (Missing component #5)
    # ----------------------------------------------------------------

    def _check_termination(self):
        """Signal all_done when we have the complete file AND every neighbor does too."""
        assert self.peer_state is not None
        if not self._completion_logged:
            return  # our own file must be assembled first
        if not self.peer_state.has_complete_file():
            return
        expected_neighbors = len(self.peers) - 1
        if self.peer_state.all_neighbors_complete(expected_neighbors):
            print(f"[PeerNetwork:{self.my_peer_id}] All {len(self.peers)} peers have the complete file – signalling termination.")
            self.all_done.set()


    # ----------------------------------------------------------------
    # Main message handler
    # ----------------------------------------------------------------

    def on_bytes_handler(self, conn: connection.Connection, data: bytes):
        with self.connections_lock:
            # Find the peer_id for this connection
            peer_id = None
            for pid, c in self.connections.items():
                if c is conn:
                    peer_id = pid
                    break
                    
            if peer_id is None:
                print(f"[PeerNetwork] Unknown connection {conn.name} received bytes.")
                return
                
            framer = self.framers.get(peer_id)
            if not framer:
                return
                
        framer.feed(data)
        
        while True:
            try:
                msg = framer.next_message()
            except Exception as e:
                print(f"[PeerNetwork] Framer parse error on {conn.name}: {e}")
                conn.close()
                break
                
            if msg is None:
                break # need more data
                
            if msg[0] == "handshake":
                remote_id = msg[1]
                print(f"[PeerNetwork] Handshake complete with peer {remote_id} via {conn.name}")
                # If this was an incoming connection (tmp_id < 0), re-key it using the real peer ID
                if peer_id < 0:
                    with self.connections_lock:
                        # Safety check in case they are already connected
                        if remote_id in self.connections:
                            print(f"[PeerNetwork] Duplicate connection detected for {remote_id}, closing.")
                            conn.close()
                            return
                        self.connections[remote_id] = conn
                        self.framers[remote_id] = framer
                        del self.connections[peer_id]
                        del self.framers[peer_id]

                    # Log incoming connection (Missing component #6)
                    if self.logger:
                        self.logger.log_connected_from(remote_id)

                # Update peer_id for subsequent message processing in this call
                peer_id = remote_id

                # Handshake finished, add to peer state
                self.peer_state.add_neighbor(remote_id)
                
                # Immediately send our bitfield if we have any pieces
                if self.peer_state.my_bitfield.count() > 0:
                    try:
                        bf_bytes = self.peer_state.my_bitfield.to_bytes()
                        bf_msg = protocol.create_message(protocol.MsgType.BITFIELD, bf_bytes)
                        conn.send(bf_msg)
                        print(f"[PeerNetwork] Sent BITFIELD to {remote_id}")
                    except Exception as e:
                        print(f"[PeerNetwork] Error sending BITFIELD to {remote_id}: {e}")
                        
            elif msg[0] == "message":
                msg_type, payload = msg[1]
                if msg_type is None:
                    # Keep-alive
                    continue
                    
                print(f"[PeerNetwork] Received message type {msg_type} payload {len(payload)}b from {conn.name}")
                
                # --------------------------------------------------------
                # BITFIELD
                # --------------------------------------------------------
                if msg_type == protocol.MsgType.BITFIELD:
                    remote_id = peer_id if peer_id > 0 else None
                    if remote_id:
                        try:
                            self.peer_state.update_neighbor_bitfield(remote_id, payload)
                            missing = self.peer_state.get_interesting_pieces(remote_id)
                            print(f"[PeerNetwork] Updated bitfield for {remote_id}. Missing {len(missing)} pieces.")
                            
                            # If we are missing pieces they have, send INTERESTED
                            if len(missing) > 0:
                                self.peer_state.set_am_interested(remote_id, True)
                                conn.send(protocol.create_message(protocol.MsgType.INTERESTED))
                                print(f"[PeerNetwork] Sent INTERESTED to {remote_id}")
                            else:
                                self.peer_state.set_am_interested(remote_id, False)
                                conn.send(protocol.create_message(protocol.MsgType.NOT_INTERESTED))
                                print(f"[PeerNetwork] Sent NOT_INTERESTED to {remote_id}")
                            
                            # A received bitfield might complete this neighbor's state
                            self._check_termination()
                        except Exception as e:
                            print(f"[PeerNetwork] Error processing BITFIELD: {e}")
                            
                # --------------------------------------------------------
                # INTERESTED  (Missing component #6 – logging)
                # --------------------------------------------------------
                elif msg_type == protocol.MsgType.INTERESTED:
                    if peer_id > 0:
                        self.peer_state.set_peer_interested(peer_id, True)
                        if self.logger:
                            # Pieces they want = pieces we have that they don't
                            self.logger.log_received_interested(peer_id)
                        print(f"[PeerNetwork] Peer {peer_id} is INTERESTED in us")
                        
                # --------------------------------------------------------
                # NOT_INTERESTED  (Missing component #6 – logging)
                # --------------------------------------------------------
                elif msg_type == protocol.MsgType.NOT_INTERESTED:
                    if peer_id > 0:
                        self.peer_state.set_peer_interested(peer_id, False)
                        if self.logger:
                            self.logger.log_received_not_interested(peer_id)
                        print(f"[PeerNetwork] Peer {peer_id} is NOT_INTERESTED in us")
                        
                # --------------------------------------------------------
                # CHOKE  (Missing component #4 – clear outstanding request)
                # --------------------------------------------------------
                elif msg_type == protocol.MsgType.CHOKE:
                    if peer_id > 0:
                        self.peer_state.set_peer_choking(peer_id, True)
                        # Clear any outstanding request from this peer since
                        # we won't be receiving that piece.
                        self.peer_state.clear_outstanding_request(peer_id)
                        if self.logger:
                            self.logger.log_choked_by(peer_id)
                        print(f"[PeerNetwork] Peer {peer_id} CHOKED us")
                        
                # --------------------------------------------------------
                # UNCHOKE  (Missing component #4 – trigger piece request)
                # --------------------------------------------------------
                elif msg_type == protocol.MsgType.UNCHOKE:
                    if peer_id > 0:
                        self.peer_state.set_peer_choking(peer_id, False)
                        if self.logger:
                            self.logger.log_unchoked_by(peer_id)
                        print(f"[PeerNetwork] Peer {peer_id} UNCHOKED us")
                        # Immediately try to request a piece from this peer
                        self._maybe_request_piece(peer_id, conn)

                # --------------------------------------------------------
                # HAVE  (Missing component #2)
                # --------------------------------------------------------
                elif msg_type == protocol.MsgType.HAVE:
                    if peer_id > 0 and len(payload) >= 4:
                        piece_index = struct.unpack(">I", payload[:4])[0]
                        needed = not self.peer_state.has_piece(piece_index)
                        self.peer_state.neighbor_set_piece(peer_id, piece_index)
                        if self.logger:
                            self.logger.log_received_have(peer_id, piece_index)
                        print(f"[PeerNetwork] Peer {peer_id} has piece {piece_index}")
                        # Re-evaluate interest
                        self._sync_interest_for_peer(peer_id, conn)
                        # Check termination – the neighbor just got a new piece
                        self._check_termination()

                # --------------------------------------------------------
                # REQUEST  (Missing component #3)
                # --------------------------------------------------------
                elif msg_type == protocol.MsgType.REQUEST:
                    if peer_id > 0 and len(payload) >= 4:
                        piece_index = struct.unpack(">I", payload[:4])[0]
                        # Only serve pieces if we have unchoked this peer
                        if not self.peer_state.is_am_choking(peer_id):
                            try:
                                piece_data = self.file_manager.read_piece(piece_index)
                                piece_payload = struct.pack(">I", piece_index) + piece_data
                                conn.send(protocol.create_message(protocol.MsgType.PIECE, piece_payload))
                                print(f"[PeerNetwork] Sent PIECE {piece_index} to {peer_id}")
                            except Exception as e:
                                print(f"[PeerNetwork] failed to serve piece {piece_index} to {peer_id}: {e}")
                        else:
                            print(f"[PeerNetwork] Ignoring REQUEST from choked peer {peer_id}")

                # --------------------------------------------------------
                # PIECE
                # --------------------------------------------------------
                elif msg_type == protocol.MsgType.PIECE:
                    if len(payload) < 4:
                        continue

                    piece_index = struct.unpack(">I", payload[:4])[0]
                    piece_data = payload[4:]
                    self._handle_piece_received(peer_id, piece_index, piece_data)
                        
                try:
                    self.on_bytes_received(conn, (msg_type, payload))
                except Exception as e:
                    print(f"[PeerNetwork] on_bytes_received handler raised: {e}")

    def on_conn_close_handler(self, conn: connection.Connection):
        with self.connections_lock:
            remove_ids = [pid for pid, c in self.connections.items() if c is conn]
            for pid in remove_ids:
                del self.connections[pid]
                if pid in self.framers:
                    del self.framers[pid]
                if pid > 0 and self.peer_state:
                    self.peer_state.remove_neighbor(pid)
        print(f"[PeerNetwork] Connection closed: {conn.name}")
        
        if self.peer_state and self._completion_logged:
            with self.connections_lock:
                no_connections_left = len(self.connections) == 0
            if no_connections_left and self.peer_state.has_complete_file():
                print(f"[PeerNetwork:{self.my_peer_id}] All connections closed and file is complete – signalling termination.")
                self.all_done.set()
            else:
                self._check_termination()

    def get_connection(self, peer_id: int) -> Optional[connection.Connection]:
        with self.connections_lock:
            return self.connections.get(peer_id)

    def stop(self):
        self.running = False
        try:
            if self.server_sock:
                self.server_sock.close()
        except:
            pass
        with self.connections_lock:
            for c in list(self.connections.values()):
                try:
                    c.close()
                except:
                    pass
            self.connections.clear()
        if self.logger:
            self.logger.close()
        print("[PeerNetwork] stopped")

#demo stuff
def demo_on_bytes(conn: connection.Connection, msg: tuple):
    msg_type, payload = msg
    print(f"[demo_on_bytes] ({conn.name}) got structured message {msg_type} ({len(payload)} bytes)")

def demo_on_connection_made(conn: connection.Connection, peerinfo: config_loader.PeerInfo):
    print(f"[demo_on_connection_made] connected to peer {peerinfo.peer_id} ({peerinfo.host}:{peerinfo.port})")

def demo_on_connection_received(conn: connection.Connection, peerinfo: config_loader.PeerInfo):
    print(f"[demo_on_connection_received] incoming connection from {conn.remote_addr}")