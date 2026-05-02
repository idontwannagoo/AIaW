#!/usr/bin/env bash
# Run Playwright e2e end-to-end:
#   1. ensure docker compose test DB is up (5434)
#   2. ensure the test backend is up on 9011 (idempotent)
#   3. build all flag profiles serially (cache hit ≈ 0.4s)
#   4. exec playwright with E2E_BUILD_DIR_* env vars pointing at each profile
#
# We build serially because build-frontend-profile.mjs runs `quasar build`
# which writes to dist/spa and swaps .env.local — three concurrent runs would
# trample each other. After the first cold run, all three are cache hits.
#
# Exit code = playwright's exit code (0 = green, 1 = red).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# 1. Postgres test DB.
bash "$REPO_ROOT/tests/scripts/test-up.sh"

# 2. Backend on 9011. backend-start.sh refuses to double-start; probe /health
# first and only start when nothing is responding.
if ! curl -sf -o /dev/null --max-time 2 'http://127.0.0.1:9011/api/v1/health'; then
  echo "[run-playwright] no backend on 9011, starting"
  bash "$REPO_ROOT/tests/scripts/backend-start.sh"
else
  echo "[run-playwright] backend on 9011 already responding, reusing"
fi

mkdir -p "$REPO_ROOT/tests/.results"

# 3. Build all profiles serially. build-frontend-profile.mjs prints the cache
# dir on its last stdout line; capture and propagate as env to playwright.
build_profile() {
  local p="$1"
  echo "[run-playwright] building profile=$p" >&2
  node "$REPO_ROOT/tests/scripts/build-frontend-profile.mjs" --profile="$p" | tail -n 1
}

export E2E_BUILD_DIR_BASELINE="$(build_profile baseline)"
export E2E_BUILD_DIR_PROVIDERS_REST="$(build_profile providers-rest)"
export E2E_BUILD_DIR_REALTIME_WS="$(build_profile realtime-ws)"
export E2E_BUILD_DIR_REALTIME_SSE="$(build_profile realtime-sse)"
export E2E_BUILD_DIR_REALTIME_POLL="$(build_profile realtime-poll)"
export E2E_BUILD_DIR_REALTIME_AUTO="$(build_profile realtime-auto)"

for var in E2E_BUILD_DIR_BASELINE E2E_BUILD_DIR_PROVIDERS_REST E2E_BUILD_DIR_REALTIME_WS E2E_BUILD_DIR_REALTIME_SSE E2E_BUILD_DIR_REALTIME_POLL E2E_BUILD_DIR_REALTIME_AUTO; do
  v="${!var}"
  if [ -z "$v" ] || [ ! -d "$v" ]; then
    echo "ERROR: $var=$v is not a valid build dir" >&2
    exit 1
  fi
done

# 4. Playwright. Pass through caller args (e.g. --project=baseline -g smoke).
exec pnpm exec playwright test "$@"
