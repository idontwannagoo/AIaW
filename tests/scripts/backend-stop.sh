#!/usr/bin/env bash
# Stop the test backend started by backend-start.sh. Idempotent: succeeds
# even if no PID file exists or the process is already gone.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PID_FILE="$REPO_ROOT/tests/.results/backend.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "[backend-stop] no pid file, nothing to stop"
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
rm -f "$PID_FILE"

if [ -z "$PID" ]; then
  echo "[backend-stop] empty pid file, removed"
  exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
  echo "[backend-stop] pid $PID not running, cleaned up"
  exit 0
fi

echo "[backend-stop] sending SIGTERM to pid $PID"
kill "$PID" 2>/dev/null || true

# Give it 5s to exit cleanly, then escalate.
for _ in $(seq 1 10); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[backend-stop] backend stopped"
    exit 0
  fi
  sleep 0.5
done

echo "[backend-stop] backend did not exit, sending SIGKILL"
kill -9 "$PID" 2>/dev/null || true
