import sys
import time
import config_loader
import peer_network

if len(sys.argv) != 2:
    print("Usage: python3 peer_network.py <my_peer_id>")
    sys.exit(1)

my_id = int(sys.argv[1])
peers = config_loader.parse_peerinfo_cfg()
common = config_loader.parse_common_cfg()
print(f"[main] Loaded {len(peers)} peers, common props sample: { {k:common[k] for k in ('FileName','FileSize','PieceSize','numPieces') if k in common} }")

pn = peer_network.PeerNetwork(my_id, peers, on_bytes_received=peer_network.demo_on_bytes,
                                on_connection_made=peer_network.demo_on_connection_made,
                                on_connection_received=peer_network.demo_on_connection_received,
                                bind_host="0.0.0.0")
try:
    pn.start()
    print("[main] PeerNetwork started. Press Ctrl-C to stop.")
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Interrupted, shutting down...")
finally:
    pn.stop()