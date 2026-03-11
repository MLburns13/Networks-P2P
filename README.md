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
- protocol.py: Contains the MessageFramer which safely buffers real-time raw TCP streams into complete structured messages and unpacks the 32-byte Handshake.
- state.py: Thread-safe state store containing Bitfield structures and tracks piece inventory and choke/interest status for the local peer and all active neighbors.
