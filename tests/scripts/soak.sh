#!/usr/bin/env bash
# Stage 2 Step 6 soak test (上线把关 RSS 漂移 + p95 延迟).
#
# Drives the running test backend (9011) with a steady PUT load while
# holding open N WebSocket subscribers, sampling backend RSS every 5 min.
# Idempotent and interruptible — Ctrl+C / SIGTERM triggers a clean shutdown
# that emits the summary line, prints the RSS series, and exits 0.
#
# Outputs (under tests/.results/):
#   soak.events.jsonl   — one JSON-Line per loadgen event (ready / put / summary)
#   soak.rss.tsv        — `epoch_s\trss_kb\tnote` time series
#   soak.summary.txt    — human-readable summary with start/end RSS + p95
#
# Usage:
#   bash tests/scripts/soak.sh                 # 1 h default
#   bash tests/scripts/soak.sh --duration 600  # 10 min (used for Step 6 capture)
#   bash tests/scripts/soak.sh --duration 300 --ws-clients 3 --rate-hz 3
#
# Pre-reqs: pnpm test:up && pnpm test:backend:start
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="$REPO_ROOT/src-backend/.venv/bin/python"
PID_FILE="$REPO_ROOT/tests/.results/backend.pid"
RESULTS="$REPO_ROOT/tests/.results"
EVENTS="$RESULTS/soak.events.jsonl"
RSS_TSV="$RESULTS/soak.rss.tsv"
SUMMARY="$RESULTS/soak.summary.txt"

DURATION=3600
WS_CLIENTS=5
RATE_HZ=5
RSS_INTERVAL=300

while [ $# -gt 0 ]; do
  case "$1" in
    --duration) DURATION="$2"; shift 2 ;;
    --ws-clients) WS_CLIENTS="$2"; shift 2 ;;
    --rate-hz) RATE_HZ="$2"; shift 2 ;;
    --rss-interval) RSS_INTERVAL="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: $VENV_PY not found. Run 'pnpm test:api:install' first." >&2
  exit 1
fi
if [ ! -f "$PID_FILE" ]; then
  echo "ERROR: backend not running (no $PID_FILE). Run 'pnpm test:backend:start' first." >&2
  exit 1
fi
BACKEND_PID="$(cat "$PID_FILE")"
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  echo "ERROR: backend pid $BACKEND_PID is not alive." >&2
  exit 1
fi

mkdir -p "$RESULTS"
: > "$EVENTS"
: > "$RSS_TSV"
: > "$SUMMARY"

sample_rss() {
  local note="${1:-}"
  local rss
  rss="$(ps -o rss= -p "$BACKEND_PID" 2>/dev/null | awk '{print $1}')"
  if [ -z "$rss" ]; then return 1; fi
  printf '%s\t%s\t%s\n' "$(date +%s)" "$rss" "$note" >> "$RSS_TSV"
  echo "[soak] rss=${rss}kb note=${note}"
}

LOADGEN_PID=""
cleanup() {
  if [ -n "$LOADGEN_PID" ] && kill -0 "$LOADGEN_PID" 2>/dev/null; then
    kill -TERM "$LOADGEN_PID" 2>/dev/null || true
    wait "$LOADGEN_PID" 2>/dev/null || true
  fi
}
finalize() {
  cleanup
  sample_rss 'end' || true
  write_summary
  echo "[soak] done. summary: $SUMMARY"
}
trap finalize EXIT
trap 'echo "[soak] caught signal, winding down..."; exit 130' INT TERM

write_summary() {
  local first_rss last_rss delta
  first_rss="$(awk 'NR==1{print $2}' "$RSS_TSV" 2>/dev/null || echo '')"
  last_rss="$(awk 'END{print $2}' "$RSS_TSV" 2>/dev/null || echo '')"
  delta=''
  if [ -n "$first_rss" ] && [ -n "$last_rss" ]; then
    delta="$((last_rss - first_rss))"
  fi
  {
    echo "soak.sh — Stage 2 Step 6 上线把关"
    echo "duration_s=${DURATION} ws_clients=${WS_CLIENTS} rate_hz=${RATE_HZ}"
    echo "backend_pid=${BACKEND_PID}"
    echo
    echo "RSS series (kb):"
    cat "$RSS_TSV" 2>/dev/null || true
    echo
    if [ -n "$delta" ]; then
      printf 'RSS first=%s kb (%.2f MB)\n' "$first_rss" "$(awk "BEGIN{print $first_rss/1024}")"
      printf 'RSS last =%s kb (%.2f MB)\n' "$last_rss"  "$(awk "BEGIN{print $last_rss/1024}")"
      printf 'RSS delta=%s kb (%.2f MB)\n' "$delta" "$(awk "BEGIN{print $delta/1024}")"
    fi
    echo
    echo 'last loadgen event:'
    tail -n 1 "$EVENTS" 2>/dev/null || true
  } > "$SUMMARY"
  cat "$SUMMARY"
}

echo "[soak] backend_pid=$BACKEND_PID duration=${DURATION}s ws=$WS_CLIENTS rate=${RATE_HZ}Hz"
sample_rss 'start'

# Launch the loadgen as a background child; we tee its stdout to events file.
"$VENV_PY" "$REPO_ROOT/tests/scripts/soak-loadgen.py" \
  --duration "$DURATION" \
  --ws-clients "$WS_CLIENTS" \
  --rate-hz "$RATE_HZ" \
  >> "$EVENTS" &
LOADGEN_PID=$!
echo "[soak] loadgen_pid=$LOADGEN_PID"

# Wait for the 'ready' line so we don't sample mid-registration.
deadline=$((SECONDS + 30))
while ! grep -q '"type":"ready"' "$EVENTS" 2>/dev/null; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "ERROR: loadgen did not emit ready within 30s" >&2
    exit 1
  fi
  if ! kill -0 "$LOADGEN_PID" 2>/dev/null; then
    echo "ERROR: loadgen died before ready. tail of events:" >&2
    tail -n 20 "$EVENTS" >&2 || true
    exit 1
  fi
  sleep 0.2
done
echo "[soak] loadgen ready"

# Periodic RSS sampling. Loop exits once loadgen exits OR DURATION elapses.
elapsed=0
sample_rss '5s-warmup'
while kill -0 "$LOADGEN_PID" 2>/dev/null; do
  step=$RSS_INTERVAL
  if [ "$elapsed" -lt 60 ]; then step=30; fi  # finer sampling early
  sleep "$step"
  elapsed=$((elapsed + step))
  sample_rss "t+${elapsed}s" || true
done

wait "$LOADGEN_PID" 2>/dev/null || true
LOADGEN_PID=''  # finalize trap won't double-kill
