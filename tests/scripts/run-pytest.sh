#!/usr/bin/env bash
# Run the pytest API layer (Phase 3) end-to-end:
#   1. ensure docker compose test DB is up (5434)
#   2. ensure the test backend is up on 9011 (idempotent)
#   3. run pytest, forwarding any extra CLI args
#
# We deliberately leave the docker DB and backend running after success so
# repeated runs in a session start fast. Use `pnpm test:down` +
# `pnpm test:backend:stop` to clean up.
#
# Exit code = pytest's exit code (0 = green, 1 = red, etc.).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

VENV_PYTHON="$REPO_ROOT/src-backend/.venv/bin/python"
VENV_PYTEST="$REPO_ROOT/src-backend/.venv/bin/pytest"

if [ ! -x "$VENV_PYTEST" ]; then
  echo "ERROR: pytest not found at $VENV_PYTEST" >&2
  echo "Hint: src-backend/.venv/bin/pip install -r src-backend/requirements-dev.txt" >&2
  exit 1
fi

# 1. Postgres test DB. test-up.sh is idempotent.
bash "$REPO_ROOT/tests/scripts/test-up.sh"

# 2. Backend on 9011. backend-start.sh refuses to double-start, so probe
# /health first; only start when nothing is responding.
if ! curl -sf -o /dev/null --max-time 2 'http://127.0.0.1:9011/api/v1/health'; then
  echo "[run-pytest] no backend on 9011, starting"
  bash "$REPO_ROOT/tests/scripts/backend-start.sh"
else
  echo "[run-pytest] backend on 9011 already responding, reusing"
fi

mkdir -p "$REPO_ROOT/tests/.results"

# 3. pytest. Pass through any caller args (e.g. `-k name`, `-x`, `-m slow`).
exec "$VENV_PYTEST" "$@"
