## CNT 5106C Project - Michael Burns, Xuhang He

# Individual Contributions

### Michael Burns

- Config & Peer info loaders
- Networking Core (Server + Outgoing Connections)

### Xuhang He

- Protocol Codec + Framing 
- State Store (Bitfields + Connection State) 

# Code Introduction

- config_loader.py: Parses Common.cfg and PeerInfo.cfg, and computes relevant constants such as numPieces = FileSize/PieceSize
- connection.py: class that handles starting connections, as well as sending and receiving messages 
- peer_network.py: Handles networks of connections with multiple peers, sending messages between them, and reconnecting/stopping connections
