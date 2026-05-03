#!/usr/bin/env bash
# Start the FastAPI backend in test mode on port 9011. Hardcodes test
# credentials/secrets so we never accidentally pick up the user's dev
# .env.local. Runs alembic upgrade head before launching uvicorn (idempotent
# — re-running on an already-migrated DB is a no-op).
#
# PID is written to tests/.results/backend.pid; logs go to
# tests/.results/backend.log. Use backend-stop.sh to shut down.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="$REPO_ROOT/src-backend/.venv/bin/python"
VENV_UVICORN="$REPO_ROOT/src-backend/.venv/bin/uvicorn"
VENV_ALEMBIC="$REPO_ROOT/src-backend/.venv/bin/alembic"

for bin in "$VENV_PY" "$VENV_UVICORN" "$VENV_ALEMBIC"; do
  if [ ! -x "$bin" ]; then
    echo "ERROR: $bin not found. Did you set up src-backend/.venv?" >&2
    echo "Hint: cd src-backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
done

mkdir -p "$REPO_ROOT/tests/.results"
PID_FILE="$REPO_ROOT/tests/.results/backend.pid"
LOG_FILE="$REPO_ROOT/tests/.results/backend.log"

# Already running?
if [ -f "$PID_FILE" ]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[backend-start] backend already running (pid=$existing_pid). Use backend-stop.sh first." >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi

# Refuse to bind 9011 if some other process owns it.
if owner="$(lsof -nP -iTCP:9011 -sTCP:LISTEN -t 2>/dev/null)" && [ -n "$owner" ]; then
  echo "ERROR: port 9011 already in use by PID $owner; refusing to start test backend." >&2
  exit 1
fi

# Test-only env. Keep these in lockstep with docker-compose.test.yml.
export DATABASE_URL='postgresql+asyncpg://aiaw:aiaw_test@localhost:5434/aiaw_test'
export JWT_SECRET='test-only-secret-do-not-use-in-prod'
export BACKEND_DATA_API_ENABLED='true'
export CORS_ALLOW_ORIGINS='http://localhost:9007,http://127.0.0.1:9007,http://localhost:9008,http://127.0.0.1:9008,http://localhost:9009,http://127.0.0.1:9009,http://localhost:9012,http://127.0.0.1:9012,http://localhost:9013,http://127.0.0.1:9013,http://localhost:9014,http://127.0.0.1:9014,http://localhost:9015,http://127.0.0.1:9015'
# Open registration in tests so conftest can spin up users without managing
# invite codes. Stage 1.5 plan defaults to invite mode, but that's a deploy
# concern, not a test concern.
export ALLOW_REGISTRATION='true'
# Stage 4.5 Step 1 — turn the ImportJob worker on in the test backend so
# tests/api/test_import_job.py can drive it (worker registers in lifespan
# only when this flag is true; off = lifespan no-op = startup_complete never
# fires for tests that touch get_worker()).
export IMPORT_JOB_ENABLED='true'

echo "[backend-start] alembic upgrade head"
( cd "$REPO_ROOT/src-backend" && "$VENV_ALEMBIC" upgrade head ) >>"$LOG_FILE" 2>&1

echo "[backend-start] launching uvicorn on 127.0.0.1:9011 (logs: tests/.results/backend.log)"
# Use pushd so $! captures the uvicorn pid directly (a `( ... ) &` subshell
# would background the subshell instead, leaving uvicorn unkillable via PID).
pushd "$REPO_ROOT/src-backend" >/dev/null
nohup "$VENV_UVICORN" app:app --host 127.0.0.1 --port 9011 --log-level info >>"$LOG_FILE" 2>&1 &
PID=$!
popd >/dev/null
echo "$PID" > "$PID_FILE"

# Wait until /health responds or the process dies.
deadline=$((SECONDS + 30))
while true; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "ERROR: backend exited during startup. Tail of log:" >&2
    tail -n 40 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -sf -o /dev/null --max-time 2 'http://127.0.0.1:9011/api/v1/health'; then
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "ERROR: backend /health did not respond within 30s. Tail of log:" >&2
    tail -n 40 "$LOG_FILE" >&2 || true
    kill "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 1
  fi
  sleep 0.5
done

echo "[backend-start] backend ready (pid=$PID, http://127.0.0.1:9011)"
