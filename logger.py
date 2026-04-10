from datetime import datetime
from pathlib import Path
import threading
from typing import Iterable

class PeerLogger:
    def __init__(self, peer_id: int, base_dir: str = "."):
        self.peer_id = peer_id
        self.path = Path(base_dir) / f"log_peer_{peer_id}.log"
        self.lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)

    def close(self) -> None:
        with self.lock:
            try:
                self._fh.close()
            except Exception:
                pass

    def _ts(self) -> str:
        return datetime.now().strftime("%m/%d/%Y %H:%M:%S")

    def log(self, message: str) -> None:
        line = f"{self._ts()}: {message}\n"
        with self.lock:
            self._fh.write(line)

    def log_connection_made(self, remote_id: int) -> None:
        self.log(f"Peer {self.peer_id} makes a connection to Peer {remote_id}.")

    def log_connected_from(self, remote_id: int) -> None:
        self.log(f"Peer {self.peer_id} is connected from Peer {remote_id}.")

    def log_preferred_neighbors(self, neighbor_ids: Iterable[int]) -> None:
        ids = ",".join(str(x) for x in neighbor_ids)
        self.log(f"Peer {self.peer_id} has the preferred neighbors {ids}.")

    def log_optimistic_neighbor(self, neighbor_id: int) -> None:
        self.log(
            f"Peer {self.peer_id} has the optimistically unchoked neighbor {neighbor_id}."
        )

    def log_unchoked_by(self, remote_id: int) -> None:
        self.log(f"Peer {self.peer_id} is unchoked by Peer {remote_id}.")

    def log_choked_by(self, remote_id: int) -> None:
        self.log(f"Peer {self.peer_id} is choked by Peer {remote_id}.")

    def log_received_have(self, remote_id: int, piece_index: int) -> None:
        self.log(
            f"Peer {self.peer_id} received the 'have' message from Peer {remote_id} "
            f"for the piece {piece_index}."
        )

    def log_received_interested(self, remote_id: int) -> None:
        self.log(f"Peer {self.peer_id} received the 'interested' message from Peer {remote_id}.")

    def log_received_not_interested(self, remote_id: int) -> None:
        self.log(
            f"Peer {self.peer_id} received the 'not interested' message from Peer {remote_id}."
        )

    def log_downloaded_piece(self, piece_index: int, remote_id: int, num_pieces: int) -> None:
        self.log(
            f"Peer {self.peer_id} has downloaded the piece {piece_index} from Peer {remote_id}. "
            f"Now the number of pieces it has is {num_pieces}."
        )

    def log_completed_file(self) -> None:
        self.log(f"Peer {self.peer_id} has downloaded the complete file.")