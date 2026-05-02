#!/usr/bin/env bash
# Bring up the isolated test Postgres on 5434. Refuses to run if 5434 is
# already busy (something else owns it) so we never silently collide with
# the user's dev stack on 5433/9010.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

check_port_free() {
  local port="$1"
  local owner
  if owner="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)" && [ -n "$owner" ]; then
    # Allow if it's already our test Postgres container (idempotent re-up).
    if docker ps --filter "name=aiaw-postgres-test" --filter "publish=$port" --format '{{.Names}}' 2>/dev/null | grep -q '^aiaw-postgres-test$'; then
      return 0
    fi
    echo "ERROR: port $port is already in use by PID $owner; refusing to start test stack." >&2
    echo "Hint: this script intentionally avoids dev ports (5433/9010/9005). Free port $port and retry." >&2
    return 1
  fi
}

check_port_free 5434

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found in PATH." >&2
  exit 1
fi

echo "[test-up] docker compose up -d (postgres-test on 5434)"
docker compose -f docker-compose.test.yml up -d

echo "[test-up] waiting for postgres healthcheck"
deadline=$((SECONDS + 60))
while true; do
  status="$(docker inspect --format '{{.State.Health.Status}}' aiaw-postgres-test 2>/dev/null || echo unknown)"
  case "$status" in
    healthy) break ;;
    unhealthy)
      echo "ERROR: aiaw-postgres-test reported unhealthy. docker logs follows:" >&2
      docker logs --tail=50 aiaw-postgres-test >&2 || true
      exit 1
      ;;
  esac
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "ERROR: postgres did not become healthy within 60s (last status: $status)" >&2
    docker logs --tail=50 aiaw-postgres-test >&2 || true
    exit 1
  fi
  sleep 1
done

echo "[test-up] postgres healthy on 5434 (db=aiaw_test user=aiaw)"
