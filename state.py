import threading
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

class Bitfield:
    """
    Manages a bitfield of a specific size.
    The PDF specific rules:
    - The first byte corresponds to piece indices 0-7 from high bit to low bit.
    - Spare bits at the end are set to zero.
    """
    def __init__(self, num_pieces: int, has_file: bool = False):
        self.num_pieces = num_pieces
        res_bytes = math.ceil(num_pieces / 8.0)
        self.data = bytearray(res_bytes)
        
        if len(self.data) > 0 and has_file:
            # Set all relevant bits to 1
            for i in range(num_pieces):
                self.set_piece(i)

    def set_piece(self, index: int):
        if index < 0 or index >= self.num_pieces:
            raise IndexError(f"Piece index {index} out of range [0, {self.num_pieces-1}]")
            
        byte_index = index // 8
        bit_offset = 7 - (index % 8) # High bit to low bit
        
        self.data[byte_index] |= (1 << bit_offset)

    def has_piece(self, index: int) -> bool:
        if index < 0 or index >= self.num_pieces:
            return False
            
        byte_index = index // 8
        bit_offset = 7 - (index % 8)
        
        return bool(self.data[byte_index] & (1 << bit_offset))

    def to_bytes(self) -> bytes:
        return bytes(self.data)

    def from_bytes(self, data: bytes):
        if len(data) != len(self.data):
            raise ValueError(f"Expected bitfield of length {len(self.data)}, got {len(data)}")
        self.data = bytearray(data)
        # Note: the PDF requires trailing spare bits to be 0, we assume the incoming payload handles that correctly
        # or we might optionally clear out bits beyond num_pieces here as a safety measure.
        self._clear_trailing_bits()

    def _clear_trailing_bits(self):
        """Forces any bits beyond num_pieces to be 0 for exact compliance."""
        if self.num_pieces == 0:
            return
            
        remainder = self.num_pieces % 8
        if remainder != 0:
            last_byte_index = len(self.data) - 1
            mask = 0xFF ^ ((1 << (8 - remainder)) - 1)
            self.data[last_byte_index] &= mask
            
    def count(self) -> int:
        """Returns the number of pieces currently owned."""
        c = 0
        for i in range(self.num_pieces):
            if self.has_piece(i):
                c += 1
        return c

@dataclass
class NeighborState:
    peer_id: int
    bitfield: Bitfield
    am_choking: bool = True       # Whether I am choking them
    am_interested: bool = False   # Whether I am interested in them
    peer_choking: bool = True     # Whether they are choking me
    peer_interested: bool = False # Whether they are interested in me
    outstanding_request: Optional[int] = None # Piece index we currently requested

class PeerState:
    def __init__(self, num_pieces: int, has_file: bool):
        self.num_pieces = num_pieces
        self.my_bitfield = Bitfield(num_pieces, has_file)
        self.neighbors: Dict[int, NeighborState] = {}
        self.lock = threading.Lock()

    def add_neighbor(self, peer_id: int):
        with self.lock:
            if peer_id not in self.neighbors:
                self.neighbors[peer_id] = NeighborState(
                    peer_id=peer_id,
                    bitfield=Bitfield(self.num_pieces, False)
                )

    def remove_neighbor(self, peer_id: int):
        with self.lock:
            if peer_id in self.neighbors:
                del self.neighbors[peer_id]

    def update_neighbor_bitfield(self, peer_id: int, bitfield_bytes: bytes):
        with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].bitfield.from_bytes(bitfield_bytes)

    def neighbor_has_piece(self, peer_id: int, piece_index: int) -> bool:
        with self.lock:
            if peer_id in self.neighbors:
                return self.neighbors[peer_id].bitfield.has_piece(piece_index)
            return False

    def mark_piece_downloaded(self, piece_index: int):
        with self.lock:
            self.my_bitfield.set_piece(piece_index)

    def set_am_choking(self, peer_id: int, is_choking: bool):
        with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].am_choking = is_choking

    def set_am_interested(self, peer_id: int, is_interested: bool):
        with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].am_interested = is_interested

    def set_peer_choking(self, peer_id: int, is_choking: bool):
        with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].peer_choking = is_choking

    def set_peer_interested(self, peer_id: int, is_interested: bool):
        with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].peer_interested = is_interested

    def set_outstanding_request(self, peer_id: int, piece_index: Optional[int]):
         with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].outstanding_request = piece_index

    def get_interesting_pieces(self, peer_id: int) -> List[int]:
        """Returns a list of piece indices the neighbor has that we do NOT have."""
        with self.lock:
            if peer_id not in self.neighbors:
                return []
            
            neighbor_bf = self.neighbors[peer_id].bitfield
            
            missing = []
            for i in range(self.num_pieces):
                if neighbor_bf.has_piece(i) and not self.my_bitfield.has_piece(i):
                    missing.append(i)
            return missing
