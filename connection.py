import socket
import threading
from typing import Callable, Optional

class Connection:
    def __init__(self, sock: socket.socket, remote_addr, on_bytes: Callable[['Connection', bytes], None],
                 on_close: Optional[Callable[['Connection'], None]] = None, name: Optional[str] = None):
        self.sock = sock
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.remote_addr = remote_addr
        self.on_bytes = on_bytes
        self.on_close = on_close
        self.lock = threading.Lock()
        self.running = True
        self.name = name or f"{remote_addr}"
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True, name=f"recv-{self.name}")

    def start(self):
        """Starts the background receive loop explicitly, so the creator has a chance to map this connection first."""
        self.recv_thread.start()

    def send(self, b: bytes):
        if not self.running:
            raise RuntimeError("Connection closed")
        with self.lock:
            total_sent = 0
            while total_sent < len(b):
                sent = self.sock.send(b[total_sent:])
                if sent == 0:
                    raise RuntimeError("socket connection broken")
                total_sent += sent

    def _recv_loop(self):
        try:
            while self.running:
                data = self.sock.recv(4096)
                if not data:
                    break

                try:
                    self.on_bytes(self, data)
                except Exception as e:
                    print(f"[Connection:{self.name}] on_bytes callback error: {e}")
        except Exception as e:
            print(f"[Connection:{self.name}] recv loop error: {e}")
        finally:
            self.running = False
            try:
                self.sock.close()
            except:
                pass
            if self.on_close:
                self.on_close(self)

    def close(self):
        self.running = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            self.sock.close()
        except:
            pass