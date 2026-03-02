#!/bin/bash
echo "Starting Peer 1001..."
python3 main.py 1001 &
PID1=$!

sleep 1

echo "Starting Peer 1002..."
python3 main.py 1002 &
PID2=$!

echo "Waiting 3 seconds for handshake to complete..."
sleep 3

echo "Stopping peers..."
kill -9 $PID1 $PID2
echo "Done."
