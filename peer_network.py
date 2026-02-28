import socket
import threading
import time
from typing import List, Callable, Optional, Dict
import config_loader
import connection

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
                conn = connection.Connection(client_sock, addr, on_bytes=self.on_bytes_handler, on_close=self.on_conn_close_handler, name=f"in-{addr}")
                
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
        try:
            self.on_bytes_received(conn, data)
        except Exception as e:
            print(f"[PeerNetwork] on_bytes_received handler raised: {e}")

    def on_conn_close_handler(self, conn: connection.Connection):
        with self.connections_lock:
            remove_ids = [pid for pid, c in self.connections.items() if c is conn]
            for pid in remove_ids:
                del self.connections[pid]
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
def demo_on_bytes(conn: connection.Connection, data: bytes):
    print(f"[demo_on_bytes] ({conn.name}) got {len(data)} bytes")
    try:
        conn.send(data)
    except Exception as e:
        print(f"[demo_on_bytes] send error: {e}")

def demo_on_connection_made(conn: connection.Connection, peerinfo: config_loader.PeerInfo):
    print(f"[demo_on_connection_made] connected to peer {peerinfo.peer_id} ({peerinfo.host}:{peerinfo.port})")

def demo_on_connection_received(conn: connection.Connection, peerinfo: config_loader.PeerInfo):
    print(f"[demo_on_connection_received] incoming connection from {conn.remote_addr}")