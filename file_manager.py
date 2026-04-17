from __future__ import annotations

from pathlib import Path


class FileManager:
    """
    Stores pieces under:
        peer_[peerID]/pieces/piece_00000.bin

    If the complete file already exists under:
        peer_[peerID]/<FileName>
    then read_piece() will read from that file.
    """

    def __init__(
        self,
        peer_id: int,
        file_name: str,
        file_size: int,
        piece_size: int,
        base_dir: str = ".",
    ):
        self.peer_id = peer_id
        self.file_name = file_name
        self.file_size = file_size
        self.piece_size = piece_size
        self.num_pieces = (file_size + piece_size - 1) // piece_size

        self.peer_dir = Path(base_dir) / "mem" / f"peer_{peer_id}"
        self.pieces_dir = self.peer_dir / "pieces"
        self.complete_path = self.peer_dir / file_name

        self.peer_dir.mkdir(parents=True, exist_ok=True)
        self.pieces_dir.mkdir(parents=True, exist_ok=True)

    def piece_length(self, index: int) -> int:
        if index < 0 or index >= self.num_pieces:
            raise IndexError(f"piece index {index} out of range")
        start = index * self.piece_size
        return min(self.piece_size, self.file_size - start)

    def piece_path(self, index: int) -> Path:
        return self.pieces_dir / f"piece_{index:05d}.bin"

    def has_complete_file(self) -> bool:
        return self.complete_path.exists()

    def has_piece(self, index: int) -> bool:
        if index < 0 or index >= self.num_pieces:
            return False
        if self.has_complete_file():
            return True
        return self.piece_path(index).exists()

    def read_piece(self, index: int) -> bytes:
        expected_len = self.piece_length(index)

        if self.complete_path.exists():
            with self.complete_path.open("rb") as f:
                f.seek(index * self.piece_size)
                data = f.read(expected_len)
            if len(data) != expected_len:
                raise IOError(
                    f"could not read piece {index}: expected {expected_len}, got {len(data)}"
                )
            return data

        path = self.piece_path(index)
        if not path.exists():
            raise FileNotFoundError(f"piece {index} not found")

        data = path.read_bytes()
        if len(data) != expected_len:
            raise IOError(
                f"piece {index} size mismatch: expected {expected_len}, got {len(data)}"
            )
        return data

    def write_piece(self, index: int, data: bytes) -> None:
        expected_len = self.piece_length(index)
        if len(data) != expected_len:
            raise ValueError(
                f"piece {index} size mismatch: expected {expected_len}, got {len(data)}"
            )
        self.piece_path(index).write_bytes(data)

    def assemble_file(self) -> Path:
        """
        Concatenate all stored pieces into peer_[peerID]/<FileName>.
        """
        if self.complete_path.exists():
            return self.complete_path

        with self.complete_path.open("wb") as out:
            for i in range(self.num_pieces):
                piece_file = self.piece_path(i)
                if not piece_file.exists():
                    raise FileNotFoundError(f"missing piece {i}")
                out.write(piece_file.read_bytes())

        return self.complete_path