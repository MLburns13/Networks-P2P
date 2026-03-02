import struct
from enum import IntEnum
from typing import Tuple, Optional

class MsgType(IntEnum):
    CHOKE = 0
    UNCHOKE = 1
    INTERESTED = 2
    NOT_INTERESTED = 3
    HAVE = 4
    BITFIELD = 5
    REQUEST = 6
    PIECE = 7

HANDSHAKE_HEADER = b"P2PFILESHARINGPROJ"
HANDSHAKE_LEN = 32

def create_handshake(peer_id: int) -> bytes:
    """
    Creates a 32-byte handshake message.
    18 bytes: 'P2PFILESHARINGPROJ'
    10 bytes: zero bits
    4 bytes: peer ID (integer representation)
    """
    # >I means big-endian 4-byte unsigned int
    return HANDSHAKE_HEADER + b'\x00' * 10 + struct.pack(">I", peer_id)

def create_message(msg_type: int, payload: bytes = b"") -> bytes:
    """
    Creates an actual message.
    4 bytes: length of (message type + payload)
    1 byte: message type
    var bytes: payload
    """
    msg_len = 1 + len(payload)
    return struct.pack(">I", msg_len) + struct.pack(">B", msg_type) + payload

class MessageFramer:
    def __init__(self):
        self.buffer = bytearray()
        self.handshake_done = False
        
    def feed(self, data: bytes):
        """Add new bytes to the buffer."""
        self.buffer.extend(data)
        
    def next_message(self) -> Optional[Tuple[str, object]]:
        """
        Attempts to parse and return the next complete message from the buffer.
        Returns:
            ("handshake", peer_id_int) -> if handshake is completed
            ("message", (msg_type_int, payload_bytes)) -> if normal message is completed
            None -> if not enough bytes are buffered
        """
        if not self.handshake_done:
            if len(self.buffer) >= HANDSHAKE_LEN:
                # We have enough bytes for the handshake
                handshake_bytes = bytes(self.buffer[:HANDSHAKE_LEN])
                self.buffer = self.buffer[HANDSHAKE_LEN:]
                
                header = handshake_bytes[:18]
                if header != HANDSHAKE_HEADER:
                    raise ValueError(f"Invalid handshake header: {header}")
                    
                peer_id = struct.unpack(">I", handshake_bytes[-4:])[0]
                self.handshake_done = True
                return ("handshake", peer_id)
            return None
        else:
            if len(self.buffer) < 4:
                return None
            
            # peek at the message length
            msg_len = struct.unpack(">I", self.buffer[:4])[0]
            
            # total bytes needed = 4 (length fields) + msg_len (type + payload)
            if len(self.buffer) >= 4 + msg_len:
                # We have a full message
                full_message = self.buffer[4:4+msg_len]
                self.buffer = self.buffer[4+msg_len:]
                
                if msg_len == 0:
                    # Optional keep-alive message handling (if supported, although project desc doesn't explicitly mention it)
                    return ("message", (None, b""))
                    
                msg_type = full_message[0]
                payload = bytes(full_message[1:])
                return ("message", (msg_type, payload))
            return None
