import unittest
import struct
from protocol import create_handshake, create_message, MessageFramer, MsgType

class TestProtocol(unittest.TestCase):
    def test_create_handshake(self):
        hs = create_handshake(1001)
        self.assertEqual(len(hs), 32)
        self.assertEqual(hs[:18], b"P2PFILESHARINGPROJ")
        self.assertEqual(hs[18:28], b"\x00" * 10)
        self.assertEqual(struct.unpack(">I", hs[-4:])[0], 1001)

    def test_create_message(self):
        msg = create_message(MsgType.HAVE, struct.pack(">I", 5))
        self.assertEqual(len(msg), 4 + 1 + 4) # len + type + payload
        
        # Read back
        msg_len = struct.unpack(">I", msg[:4])[0]
        self.assertEqual(msg_len, 5) # 1 byte type + 4 byte payload
        self.assertEqual(msg[4], MsgType.HAVE)
        self.assertEqual(struct.unpack(">I", msg[5:])[0], 5)

    def test_framer_streaming(self):
        framer = MessageFramer()
        
        # 1. Feed partial handshake
        hs = create_handshake(1001)
        framer.feed(hs[:10])
        self.assertIsNone(framer.next_message())
        
        framer.feed(hs[10:])
        msg = framer.next_message()
        self.assertIsNotNone(msg)
        self.assertEqual(msg[0], "handshake")
        self.assertEqual(msg[1], 1001)
        
        self.assertIsNone(framer.next_message()) # buffer empty
        
        # 2. Feed an exact message
        m1 = create_message(MsgType.INTERESTED, b"")
        framer.feed(m1)
        msg = framer.next_message()
        self.assertEqual(msg[0], "message")
        self.assertEqual(msg[1][0], MsgType.INTERESTED)
        self.assertEqual(msg[1][1], b"")
        
        # 3. Feed multiple messages at once (fragmented randomly)
        m2 = create_message(MsgType.HAVE, struct.pack(">I", 99))
        m3 = create_message(MsgType.REQUEST, struct.pack(">I", 123))
        
        combo = m2 + m3
        framer.feed(combo[:3])
        self.assertIsNone(framer.next_message())
        framer.feed(combo[3:])
        
        # Should pop m2
        ans = framer.next_message()
        self.assertEqual(ans[0], "message")
        self.assertEqual(ans[1][0], MsgType.HAVE)
        self.assertEqual(struct.unpack(">I", ans[1][1])[0], 99)
        
        # Should pop m3
        ans = framer.next_message()
        self.assertEqual(ans[0], "message")
        self.assertEqual(ans[1][0], MsgType.REQUEST)
        self.assertEqual(struct.unpack(">I", ans[1][1])[0], 123)
        
        # Empty again
        self.assertIsNone(framer.next_message())

if __name__ == '__main__':
    unittest.main()
