import threading
import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

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
    
    def complete(self) -> bool:
        return self.count() == self.num_pieces

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
        self.requested_pieces: Set[int] = set()
        self.download_bytes: Dict[int, int] = {}  # peer_id -> bytes downloaded this interval
        self.lock = threading.Lock()
        # Peers we know have finished (persists even after the connection closes)
        self._completed_neighbor_ids: Set[int] = set()
        if has_file:
            # Seeder counts itself as starting with all pieces; no self-entry needed
            pass

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
                # Before removing, check if this neighbor was complete so we
                # don't lose the completion info when the connection closes.
                ns = self.neighbors[peer_id]
                if ns.bitfield.complete():
                    self._completed_neighbor_ids.add(peer_id)
                if ns.outstanding_request is not None:
                    self.requested_pieces.discard(ns.outstanding_request)
                    ns.outstanding_request = None
                del self.neighbors[peer_id]

    def update_neighbor_bitfield(self, peer_id: int, bitfield_bytes: bytes):
        with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].bitfield.from_bytes(bitfield_bytes)
                # Eagerly record completion so it survives disconnection
                if self.neighbors[peer_id].bitfield.complete():
                    self._completed_neighbor_ids.add(peer_id)

    def neighbor_has_piece(self, peer_id: int, piece_index: int) -> bool:
        with self.lock:
            if peer_id in self.neighbors:
                return self.neighbors[peer_id].bitfield.has_piece(piece_index)
            return False
    
    def neighbor_set_piece(self, peer_id: int, piece_index: int):
        with self.lock:
            if peer_id in self.neighbors:
                self.neighbors[peer_id].bitfield.set_piece(piece_index)
                # Eagerly record completion so it survives disconnection
                if self.neighbors[peer_id].bitfield.complete():
                    self._completed_neighbor_ids.add(peer_id)

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
    

    def select_random_piece(self, peer_id: int) -> Optional[int]:
        with self.lock:
            ns = self.neighbors.get(peer_id)
            if ns is None:
                return None

            candidates = [
                i
                for i in range(self.num_pieces)
                if ns.bitfield.has_piece(i)
                and not self.my_bitfield.has_piece(i)
                and i not in self.requested_pieces
            ]

            if not candidates:
                # DEBUG: Log why we have no candidates
                neighbor_count = sum(1 for i in range(self.num_pieces) if ns.bitfield.has_piece(i))
                my_count = self.my_bitfield.count()
                pieces_neighbor_has_we_dont = [
                    i for i in range(self.num_pieces)
                    if ns.bitfield.has_piece(i) and not self.my_bitfield.has_piece(i)
                ]
                pieces_requested_that_neighbor_has = [
                    i for i in self.requested_pieces
                    if ns.bitfield.has_piece(i) and not self.my_bitfield.has_piece(i)
                ]
                print(f"[SELECT-PIECE-DEBUG] Peer has no candidates from {peer_id}: "
                      f"neighbor_pieces={neighbor_count}, my_pieces={my_count}, "
                      f"needed_from_neighbor={len(pieces_neighbor_has_we_dont)}, "
                      f"requested_count={len(self.requested_pieces)}, "
                      f"requested_from_this_neighbor={len(pieces_requested_that_neighbor_has)}")
                if pieces_requested_that_neighbor_has:
                    print(f"  → Blocked pieces from {peer_id}: {pieces_requested_that_neighbor_has[:10]}...")
                return None

            return random.choice(candidates)
        
    def reserve_request(self, peer_id: int, piece_index: int) -> bool:
        with self.lock:
            ns = self.neighbors.get(peer_id)
            if ns is None:
                return False
            if ns.outstanding_request is not None:
                return False
            if piece_index in self.requested_pieces:
                return False
            ns.outstanding_request = piece_index
            self.requested_pieces.add(piece_index)
            return True


    def clear_outstanding_request(self, peer_id: int):
        with self.lock:
            ns = self.neighbors.get(peer_id)
            if ns is None:
                return
            if ns.outstanding_request is not None:
                self.requested_pieces.discard(ns.outstanding_request)
                ns.outstanding_request = None

    def get_outstanding_request(self, peer_id: int) -> Optional[int]:
        with self.lock:
            ns = self.neighbors.get(peer_id)
            return None if ns is None else ns.outstanding_request

    def mark_piece_downloaded(self, piece_index: int) -> int:
        with self.lock:
            self.my_bitfield.set_piece(piece_index)
            # CRITICAL: Remove from requested_pieces if it's there
            # This prevents stalls where a piece is downloaded but stays blocked in requested_pieces
            self.requested_pieces.discard(piece_index)
            return self.my_bitfield.count()

    def has_complete_file(self) -> bool:
        with self.lock:
            return self.my_bitfield.complete()

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
                
    def is_peer_choking_us(self, peer_id: int) -> bool:
        with self.lock:
            ns = self.neighbors.get(peer_id)
            return True if ns is None else ns.peer_choking

    def is_peer_interested_in_us(self, peer_id: int) -> bool:
        with self.lock:
            ns = self.neighbors.get(peer_id)
            return False if ns is None else ns.peer_interested

    def is_am_interested_in_peer(self, peer_id: int) -> bool:
        with self.lock:
            ns = self.neighbors.get(peer_id)
            return False if ns is None else ns.am_interested

    def is_am_choking(self, peer_id: int) -> bool:
        with self.lock:
            ns = self.neighbors.get(peer_id)
            return True if ns is None else ns.am_choking

    # ---- download-rate tracking ----

    def record_download(self, peer_id: int, num_bytes: int):
        """Record bytes downloaded from a peer for rate calculation."""
        with self.lock:
            self.download_bytes[peer_id] = self.download_bytes.get(peer_id, 0) + num_bytes

    def get_and_reset_download_rates(self) -> Dict[int, int]:
        """Return accumulated byte counts per peer and reset counters."""
        with self.lock:
            rates = dict(self.download_bytes)
            self.download_bytes.clear()
            return rates

    # ---- unchoking helpers ----

    def get_interested_neighbors(self) -> List[int]:
        """Return IDs of neighbors that are interested in us."""
        with self.lock:
            return [pid for pid, ns in self.neighbors.items() if ns.peer_interested]

    def get_choked_interested_neighbors(self, exclude: Optional[Set[int]] = None) -> List[int]:
        """Return neighbor IDs that we are choking AND that are interested in us."""
        with self.lock:
            return [
                pid for pid, ns in self.neighbors.items()
                if ns.am_choking and ns.peer_interested
                and (exclude is None or pid not in exclude)
            ]

    def get_neighbor_ids(self) -> List[int]:
        with self.lock:
            return list(self.neighbors.keys())

    def all_neighbors_complete(self, expected_count: int = 0) -> bool:
        """Returns True when every peer in the swarm that we've ever tracked is done.

        A peer is considered done if:
        - Its current bitfield is complete (still connected), OR
        - It was seen complete before its connection was closed.

        We don't require expected_count active connections to exist anymore;
        instead we check that the union of currently-complete + historically-complete
        covers at least expected_count distinct peers.
        """
        with self.lock:
            # Peers that are currently connected and done
            currently_complete = {
                pid for pid, ns in self.neighbors.items() if ns.bitfield.complete()
            }
            # Union with peers that completed before disconnecting
            all_seen_complete = currently_complete | self._completed_neighbor_ids
            return len(all_seen_complete) >= expected_count


    # ---- alias used by older tests ----

    def select_random_interesting_piece(self, peer_id: int) -> Optional[int]:
        """Alias for select_random_piece (for backward compat with tests)."""
        return self.select_random_piece(peer_id)

    def get_interesting_pieces_for(self, peer_id: int) -> List[int]:
        """Return piece indices we have that the given neighbor does NOT have.
        Used to describe what they would gain by downloading from us."""
        with self.lock:
            ns = self.neighbors.get(peer_id)
            if ns is None:
                return []
            result = []
            for i in range(self.my_bitfield.num_pieces):
                if self.my_bitfield.has_piece(i) and not ns.bitfield.has_piece(i):
                    result.append(i)
            return result

    def get_neighbor_piece_count(self, peer_id: int) -> int:
        """Return how many pieces the given neighbor currently has."""
        with self.lock:
            ns = self.neighbors.get(peer_id)
            if ns is None:
                return 0
            return sum(1 for i in range(ns.bitfield.num_pieces) if ns.bitfield.has_piece(i))

    def has_piece(self, piece_index: int) -> bool:
        """Return True if we locally own piece_index."""
        with self.lock:
            return self.my_bitfield.has_piece(piece_index)