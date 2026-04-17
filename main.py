import sys
import os
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
    print("[main] PeerNetwork started. Waiting for all peers to complete ...")
    # Block until all peers have the complete file, or Ctrl-C
    while not pn.all_done.wait(timeout=1.0):
        pass
    print("[main] All peers have the complete file. Shutting down.")
    # Wait briefly before tearing down sockets so our final HAVE messages
    # have time to be flushed to the OS and delivered to our neighbors.
    # Otherwise os._exit(0) might cause TCP RST and drop outbound buffers.
    time.sleep(15.0)

except KeyboardInterrupt:
    print("Interrupted, shutting down...")
finally:
    pn.stop()

# Force immediate exit to prevent daemon threads from blocking interpreter shutdown
# and causing Fatal Python errors due to _io.BufferedWriter lock contention on stdout.
os._exit(0)