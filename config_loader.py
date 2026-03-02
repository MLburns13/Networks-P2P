import math
import os
from dataclasses import dataclass
from typing import List, Dict, Tuple

@dataclass
class PeerInfo:
    peer_id: int
    host: str
    port: int
    has_file: bool

def _parse_kv_line(line: str) -> Tuple[str, str]:
    line = line.strip()
    if not line:
        raise ValueError("empty line")

    if '=' in line:
        parts = line.split('=', 1)
        key = parts[0].strip()
        value = parts[1].strip()
        if not key:
            raise ValueError("missing key in kv pair")
        return key, value

    parts = line.split(None, 1)
    if len(parts) == 1:
        raise ValueError("expected 'key value' or 'key=value'")
    return parts[0].strip(), parts[1].strip()

def parse_common_cfg(path: str = os.path.join("cfg", "Common.cfg")) -> Dict[str, object]:
    props = {}
    with open(path, 'r', encoding='utf-8') as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            
            try:
                key, val = _parse_kv_line(line)
            except ValueError as e:
                raise ValueError(f"{path}:{lineno}: invalid line: {e}")

            if key.lower() in ("filesize", "piecesize", "unchokinginterval", "optimisticunchokinginterval"):
                try:
                    props[key] = int(val)
                except ValueError:
                    raise ValueError(f"{path}:{lineno}: expected integer for {key}, got: {val!r}")
            else:
                props[key] = val

    file_size = int(props["FileSize"])
    piece_size = int(props["PieceSize"])
    num_pieces = math.ceil(file_size / piece_size)
    props["numPieces"] = num_pieces
    
    return props

def parse_peerinfo_cfg(path: str = os.path.join("cfg", "PeerInfo.cfg")) -> List[PeerInfo]:
    peers: List[PeerInfo] = []
    seen_ids = set()
    with open(path, 'r', encoding='utf-8') as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            parts = line.split()
            pid = int(parts[0])
            host = parts[1]
            port = int(parts[2])
            has_file_token = parts[3]
            has_file = (has_file_token == '1')
            
            if pid in seen_ids:
                raise ValueError(f"{path}:{lineno}: duplicate peerId {pid}")
            seen_ids.add(pid)
            peers.append(PeerInfo(pid, host, port, has_file))
            
    if not peers:
        raise ValueError(f"{path}: no peers found")
    return peers