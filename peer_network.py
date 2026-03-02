import socket
import threading
import time
from typing import List, Callable, Optional, Dict
import config_loader
import connection
import protocol
import state

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
        self.outgoing_retry_interval = 1.0
        self.outgoing_retry_attempts = 10

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
        common = config_loader.parse_common_cfg()
        num_pieces = common['numPieces']
        self.peer_state = state.PeerState(num_pieces, my_entry.has_file)
        
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.bind_host, my_entry.port))
        self.server_sock.listen(50)
        self.running = True
        self.server_thread = threading.Thread(target=self._accept_loop, daemon=True, name="accept-loop")
        self.server_thread.start()
        print(f"[PeerNetwork] Listening on {self.bind_host}:{my_entry.port} (peer {self.my_peer_id})")

        #connect to all prior peers in the file ordering
        threading.Thread(target=self._connect_to_previous_peers, daemon=True).start()

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
                    
                conn.start()
                    
                # Send our handshake right away
                try:
                    conn.send(protocol.create_handshake(self.my_peer_id))
                except Exception as e:
                    print(f"[PeerNetwork] Error sending handshake: {e}")
                
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
            attempt = 0
            connected = False
            
            while attempt < self.outgoing_retry_attempts and not connected and self.running:
                attempt += 1
                try:
                    sock = socket.create_connection((peer_to.host, peer_to.port), timeout=5)
                    conn = connection.Connection(sock, (peer_to.host, peer_to.port), on_bytes=self.on_bytes_handler, on_close=self.on_conn_close_handler, name=f"out-{peer_to.peer_id}")
                    
                    with self.connections_lock:
                        self.connections[peer_to.peer_id] = conn
                        self.framers[peer_to.peer_id] = protocol.MessageFramer()
                    
                    conn.start()
                        
                    # Send our handshake right away
                    try:
                        conn.send(protocol.create_handshake(self.my_peer_id))
                    except Exception as e:
                        print(f"[PeerNetwork] Error sending handshake: {e}")
                        
                    print(f"[PeerNetwork] Outgoing connection established to {peer_to.peer_id}@{peer_to.host}:{peer_to.port}")
                    
                    if self.on_connection_made:
                        try:
                            self.on_connection_made(conn, peer_to)
                        except Exception as e:
                            print(f"[PeerNetwork] on_connection_made callback error: {e}")
                            
                    connected = True
                except Exception as e:
                    print(f"[PeerNetwork] Could not connect to {peer_to.peer_id}@{peer_to.host}:{peer_to.port} (attempt {attempt}): {e}")
                    time.sleep(self.outgoing_retry_interval)
                    
            if not connected:
                print(f"[PeerNetwork] FAILED to connect to {peer_to.peer_id} after {self.outgoing_retry_attempts} attempts")

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
                
                # Check for state-related messages
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
                        except Exception as e:
                            print(f"[PeerNetwork] Error processing BITFIELD: {e}")
                            
                elif msg_type == protocol.MsgType.INTERESTED:
                    if peer_id > 0:
                        self.peer_state.set_peer_interested(peer_id, True)
                        print(f"[PeerNetwork] Peer {peer_id} is INTERESTED in us")
                        
                elif msg_type == protocol.MsgType.NOT_INTERESTED:
                    if peer_id > 0:
                        self.peer_state.set_peer_interested(peer_id, False)
                        print(f"[PeerNetwork] Peer {peer_id} is NOT_INTERESTED in us")
                        
                elif msg_type == protocol.MsgType.CHOKE:
                    if peer_id > 0:
                        self.peer_state.set_peer_choking(peer_id, True)
                        print(f"[PeerNetwork] Peer {peer_id} CHOKED us")
                        
                elif msg_type == protocol.MsgType.UNCHOKE:
                    if peer_id > 0:
                        self.peer_state.set_peer_choking(peer_id, False)
                        print(f"[PeerNetwork] Peer {peer_id} UNCHOKED us")
                        
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
        print("[PeerNetwork] stopped")

#demo stuff
def demo_on_bytes(conn: connection.Connection, msg: tuple):
    msg_type, payload = msg
    print(f"[demo_on_bytes] ({conn.name}) got structured message {msg_type} ({len(payload)} bytes)")

def demo_on_connection_made(conn: connection.Connection, peerinfo: config_loader.PeerInfo):
    print(f"[demo_on_connection_made] connected to peer {peerinfo.peer_id} ({peerinfo.host}:{peerinfo.port})")

def demo_on_connection_received(conn: connection.Connection, peerinfo: config_loader.PeerInfo):
    print(f"[demo_on_connection_received] incoming connection from {conn.remote_addr}")