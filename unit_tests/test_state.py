import unittest
from state import Bitfield, PeerState

class TestStateStore(unittest.TestCase):
    def test_bitfield_padding(self):
        # PDF states: 0-7 from high to low bit. Next byte is 8-15.
        bf = Bitfield(10, False)
        # 10 pieces means 2 bytes. 
        self.assertEqual(len(bf.to_bytes()), 2)
        
        bf.set_piece(0) # MSB of byte 0
        bf.set_piece(7) # LSB of byte 0
        bf.set_piece(8) # MSB of byte 1
        
        b = bf.to_bytes()
        # Byte 0: piece 0 (high) + piece 7 (low) -> 10000001 -> 129
        self.assertEqual(b[0], 129)
        # Byte 1: piece 8 (high) -> 10000000 -> 128
        self.assertEqual(b[1], 128)
        
    def test_bitfield_clears_trailing(self):
        bf = Bitfield(10, False)
        # Simulate incoming malformed bitfield with dirty trailing bits
        bf.from_bytes(bytes([0xFF, 0xFF])) # Both bytes fully set
        
        # Internally from_bytes should clear bits 10-15 (since valid are 0-9)
        # Valid pieces in byte 1: piece 8, piece 9. The mask for keeping top 2 bits is 11000000 -> 192 (0xC0)
        b = bf.to_bytes()
        self.assertEqual(b[1], 192)

    def test_peerstate_interest(self):
        ps = PeerState(num_pieces=10, has_file=False)
        ps.add_neighbor(1001)
        
        # Let's say we have pieces 0, 1
        ps.mark_piece_downloaded(0)
        ps.mark_piece_downloaded(1)
        
        # Neighbor tells us they have 0, 1, 2, 9
        inc_bf = Bitfield(10, False)
        inc_bf.set_piece(0)
        inc_bf.set_piece(1)
        inc_bf.set_piece(2)
        inc_bf.set_piece(9)
        
        ps.update_neighbor_bitfield(1001, inc_bf.to_bytes())
        
        missing = ps.get_interesting_pieces(1001)
        self.assertEqual(missing, [2, 9]) # We have 0,1 so we just need 2 and 9
        
    def test_neighbor_tracking(self):
        ps = PeerState(num_pieces=10, has_file=False)
        ps.add_neighbor(1001)
        
        self.assertTrue(ps.neighbors[1001].am_choking)
        self.assertFalse(ps.neighbors[1001].am_interested)
        
        ps.set_am_choking(1001, False)
        ps.set_peer_interested(1001, True)
        
        self.assertFalse(ps.neighbors[1001].am_choking)
        self.assertTrue(ps.neighbors[1001].peer_interested)
        
if __name__ == '__main__':
    unittest.main()
