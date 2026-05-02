#!/usr/bin/env bash
# Tear down the test Postgres container *and* the named volume so the next
# up starts from an empty schema. Idempotent: succeeds even if nothing is
# running.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found in PATH." >&2
  exit 1
fi

echo "[test-down] docker compose down -v"
docker compose -f docker-compose.test.yml down -v --remove-orphans

echo "[test-down] done (port 5434 released, volume aiaw_test_pg removed)"
