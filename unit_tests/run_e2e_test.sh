#!/usr/bin/env bash
# ============================================================
#  End-to-end P2P test on localhost
#
#  Usage:
#    ./run_e2e_test.sh              # default: 3 peers, small file
#    ./run_e2e_test.sh  --full      # 6 peers, uses project cfg/
#    ./run_e2e_test.sh  --peers 4   # 4 peers, small file
#
#  All runtime artifacts (peer_*/, log files, cfg/) are created
#  inside unit_tests/e2e_output/ to keep the project root clean.
# ============================================================
set -euo pipefail

# ── resolve paths ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TS=$(date +%Y%m%d_%H%M%S)
WORK_DIR="$SCRIPT_DIR/e2e_output_${TS}"

# ── defaults ──
NUM_PEERS=3
FILE_SIZE=65536          # 64 KB – transfers in seconds
PIECE_SIZE=8192          # 8 KB pieces → 8 pieces
UNCHOKE_INT=2            # speed up for testing
OPT_UNCHOKE_INT=4
NUM_PREFERRED=2
FULL_MODE=false
BASE_PORT=7100           # starting port (each peer gets BASE_PORT + index)

# ── parse args ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)   FULL_MODE=true; shift ;;
    --peers)  NUM_PEERS="$2"; shift 2 ;;
    *)        echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── kill any zombie peer processes from a previous run ──
# Use lsof to find PIDs actually holding our port range – more reliable than
# pgrep pattern matching, which can fail when the binary path contains spaces
# or when the Python interpreter name differs across environments.
PORT_LO=$((BASE_PORT + 1))
PORT_HI=$((BASE_PORT + NUM_PEERS))

echo "=== Releasing any processes on ports ${PORT_LO}-${PORT_HI} ==="
HELD_PIDS=$(lsof -iTCP -sTCP:LISTEN -n -P 2>/dev/null \
  | awk -v lo="$PORT_LO" -v hi="$PORT_HI" \
      'NR>1{ split($9,a,":"); p=a[length(a)]+0; if(p>=lo && p<=hi) print $2 }' \
  | sort -u)

if [[ -n "$HELD_PIDS" ]]; then
  echo "  Found PIDs holding our ports: $(echo $HELD_PIDS | tr '\n' ' ')"
  echo "$HELD_PIDS" | xargs kill -9 2>/dev/null || true
  echo "  Sent SIGKILL – waiting up to 5s for OS to reclaim ports ..."
  sleep 2
fi

# Poll until all target ports are confirmed free (up to 15s total)
echo "=== Verifying ports ${PORT_LO}-${PORT_HI} are free ==="
DEADLINE=$((SECONDS + 15))
while true; do
  BUSY=$(lsof -iTCP -sTCP:LISTEN -n -P 2>/dev/null \
         | awk -v lo="$PORT_LO" -v hi="$PORT_HI" \
             'NR>1{split($9,a,":"); p=a[length(a)]+0; if(p>=lo && p<=hi) print p}' \
         | sort -un | tr '\n' ' ')
  if [[ -z "$BUSY" ]]; then
    echo "  All ports are free."
    break
  fi
  if [[ $SECONDS -ge $DEADLINE ]]; then
    echo "  WARNING: ports still busy after 15s: $BUSY"
    echo "  Attempting one more force-kill ..."
    # One final attempt: look up PIDs again and force-kill
    lsof -iTCP -sTCP:LISTEN -n -P 2>/dev/null \
      | awk -v lo="$PORT_LO" -v hi="$PORT_HI" \
          'NR>1{ split($9,a,":"); p=a[length(a)]+0; if(p>=lo && p<=hi) print $2 }' \
      | sort -u | xargs kill -9 2>/dev/null || true
    sleep 2
    break
  fi
  echo "  Ports still busy: $BUSY – waiting 1s ..."
  sleep 1
done

# ── prepare working directory ──
echo "=== Working directory: $WORK_DIR ==="
mkdir -p "$WORK_DIR/cfg"
mkdir -p "$WORK_DIR/mem"
mkdir -p "$WORK_DIR/logs"

# ── write config files ──
if $FULL_MODE; then
  echo "=== FULL MODE: copying project cfg/ ==="
  cp "$PROJECT_DIR/cfg/Common.cfg"  "$WORK_DIR/cfg/Common.cfg"
  cp "$PROJECT_DIR/cfg/PeerInfo.cfg" "$WORK_DIR/cfg/PeerInfo.cfg"

  NUM_PEERS=$(wc -l < "$WORK_DIR/cfg/PeerInfo.cfg" | tr -d ' ')
  FILE_NAME=$(grep FileName "$WORK_DIR/cfg/Common.cfg" | awk '{print $2}')
  FILE_SIZE=$(grep FileSize "$WORK_DIR/cfg/Common.cfg" | awk '{print $2}')
else
  FILE_NAME="TestFile.dat"

  echo "=== Generating test config: ${NUM_PEERS} peers, ${FILE_SIZE} bytes, ${PIECE_SIZE}-byte pieces ==="

  cat > "$WORK_DIR/cfg/Common.cfg" <<EOF
NumberOfPreferredNeighbors ${NUM_PREFERRED}
UnchokingInterval ${UNCHOKE_INT}
OptimisticUnchokingInterval ${OPT_UNCHOKE_INT}
FileName ${FILE_NAME}
FileSize ${FILE_SIZE}
PieceSize ${PIECE_SIZE}
EOF

  > "$WORK_DIR/cfg/PeerInfo.cfg"
  for i in $(seq 1 "$NUM_PEERS"); do
    PID=$((1000 + i))
    PORT=$((BASE_PORT + i))
    HAS_FILE=0
    [[ $i -eq 1 ]] && HAS_FILE=1
    echo "${PID} localhost ${PORT} ${HAS_FILE}" >> "$WORK_DIR/cfg/PeerInfo.cfg"
  done
fi

echo "--- cfg/Common.cfg ---"
cat "$WORK_DIR/cfg/Common.cfg"
echo ""
echo "--- cfg/PeerInfo.cfg ---"
cat "$WORK_DIR/cfg/PeerInfo.cfg"
echo ""

# ── set up seeder directory with the file ──
SEEDER_ID=$(head -1 "$WORK_DIR/cfg/PeerInfo.cfg" | awk '{print $1}')
SEEDER_DIR="$WORK_DIR/mem/peer_${SEEDER_ID}"
mkdir -p "$SEEDER_DIR"

echo "=== Creating ${FILE_SIZE}-byte random test file ==="
dd if=/dev/urandom of="${SEEDER_DIR}/${FILE_NAME}" bs=1 count="$FILE_SIZE" 2>/dev/null

SEEDER_MD5=$(md5 -q "${SEEDER_DIR}/${FILE_NAME}" 2>/dev/null || md5sum "${SEEDER_DIR}/${FILE_NAME}" | awk '{print $1}')
echo "Seeder file MD5: ${SEEDER_MD5}"
echo ""

# ── launch peers (cwd = WORK_DIR so all artifacts land there) ──
PIDS_ARRAY=()
echo "=== Starting ${NUM_PEERS} peer processes ==="

FIRST=true
while IFS= read -r line; do
  PEER_ID=$(echo "$line" | awk '{print $1}')
  echo "  → Launching peer ${PEER_ID} ..."
  (cd "$WORK_DIR" && PYTHONPATH="$PROJECT_DIR" python3 "$PROJECT_DIR/main.py" "$PEER_ID") &
  PIDS_ARRAY+=($!)
  if $FIRST; then
    # Give the seeder extra time to bind and start listening before the
    # connection storm begins. Scale with network size.
    HEAD_START=$(awk "BEGIN {s=1.0 + $NUM_PEERS*0.01; if(s>5)s=5; printf \"%.1f\",s}")
    echo "  (waiting ${HEAD_START}s for seeder to be ready...)"
    sleep "$HEAD_START"
    FIRST=false
  else
    sleep 0.1   # small stagger between leechers; backoff handles any bursts
  fi
done < "$WORK_DIR/cfg/PeerInfo.cfg"

echo ""
echo "=== All peers launched. Waiting for completion (Ctrl-C to abort) ==="
echo "    PIDs: ${PIDS_ARRAY[*]}"
echo ""

# ── wait for all processes ──
ALL_OK=true
for pid in "${PIDS_ARRAY[@]}"; do
  if ! wait "$pid" 2>/dev/null; then
    echo "  ⚠  Process $pid exited with error"
    ALL_OK=false
  fi
done

echo ""
echo "================================================================"
echo "=== All peer processes have exited.  Verifying results ...   ==="
echo "================================================================"
echo ""

# ── verify files ──
PASS=true
while IFS= read -r line; do
  PEER_ID=$(echo "$line" | awk '{print $1}')
  FILE_PATH="$WORK_DIR/mem/peer_${PEER_ID}/${FILE_NAME}"

  if [[ ! -f "$FILE_PATH" ]]; then
    echo "  ✗  peer_${PEER_ID}: file NOT found"
    PASS=false
    continue
  fi

  LEECHER_MD5=$(md5 -q "$FILE_PATH" 2>/dev/null || md5sum "$FILE_PATH" | awk '{print $1}')
  if [[ "$LEECHER_MD5" == "$SEEDER_MD5" ]]; then
    echo "  ✓  peer_${PEER_ID}: MD5 matches  (${LEECHER_MD5})"
  else
    echo "  ✗  peer_${PEER_ID}: MD5 MISMATCH  (expected ${SEEDER_MD5}, got ${LEECHER_MD5})"
    PASS=false
  fi
done < "$WORK_DIR/cfg/PeerInfo.cfg"

echo ""

# ── show log summaries ──
echo "=== Log summaries ==="
for f in "$WORK_DIR"/logs/log_peer_*.log; do
  [[ -f "$f" ]] || continue
  LINES=$(wc -l < "$f" | tr -d ' ')
  COMPLETED=$(grep -c "has downloaded the complete file" "$f" || true)
  echo "  $(basename "$f"): ${LINES} lines, completed=${COMPLETED}"
done

echo ""

if $PASS; then
  echo "══════════════════════════════════════"
  echo "   ✅  END-TO-END TEST PASSED"
  echo "══════════════════════════════════════"
else
  echo "══════════════════════════════════════"
  echo "   ❌  END-TO-END TEST FAILED"
  echo "══════════════════════════════════════"
  exit 1
fi
