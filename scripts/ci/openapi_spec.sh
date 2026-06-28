#!/usr/bin/env bash
# Generate or drift-check the committed analytics-api OpenAPI spec.
#
#   bash scripts/ci/openapi_spec.sh update   # regenerate docs/.../openapi.json
#   bash scripts/ci/openapi_spec.sh check    # exit 2 if the committed copy drifted
#
# Boots a throwaway MariaDB + analytics-api (scripts/ci/compose.metric-coverage.yml),
# fetches GET /openapi.json from the live router, and writes/diffs the committed
# copy via scripts/ci/openapi_spec.py (host needs only httpx). Reuses the metric
# gate's 2-service compose — /openapi.json is a static read, no real data needed.
#
# Fast path: if $ANALYTICS_API_URL is already set, this talks to that instance
# directly and does NOT touch Docker (for a developer with a running analytics-api).
#
# Env knobs (same contract as metric_coverage.sh):
#   ANALYTICS_API_IMAGE  prebuilt image to use instead of building from src/backend.
#   BUILD_CACHE_FROM     buildx --cache-from spec (e.g. type=gha,scope=analytics-api-amd64).
#   ANALYTICS_API_PORT   host port for analytics-api (default 18081).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

MODE="${1:?usage: openapi_spec.sh <update|check>}"
case "$MODE" in
    update) PY_MODE=write ;;
    check)  PY_MODE=check ;;
    *) echo "unknown mode: $MODE (expected: update | check)" >&2; exit 2 ;;
esac

run_py() { python3 scripts/ci/openapi_spec.py "$PY_MODE"; }

# Fast path: an analytics-api URL was provided — skip Docker entirely.
if [ -n "${ANALYTICS_API_URL:-}" ]; then
    echo "using existing analytics-api at $ANALYTICS_API_URL"
    set +e; run_py; rc=$?; set -e
    exit "$rc"
fi

# RULE-DEFAULTS-OK: loopback publish port for a throwaway container this script
# both starts and connects to — no external resource, no data-misroute risk.
PORT="${ANALYTICS_API_PORT:-18081}"
TENANT="00000000-0000-0000-0000-000000000001"
PROJECT="insight-openapi-spec"
COMPOSE=(docker compose -f scripts/ci/compose.metric-coverage.yml -p "$PROJECT")

# Ephemeral per-run credentials (the DB is torn down at exit).
export MARIADB_ROOT_PASSWORD="$(openssl rand -hex 12)"
export MARIADB_PASSWORD="$(openssl rand -hex 12)"
export ANALYTICS_API_PORT="$PORT"

# Build the analytics-api image from the CURRENT source unless one was provided
# (so the spec reflects the PR's routes, not a stale published image).
if [ -z "${ANALYTICS_API_IMAGE:-}" ]; then
    export ANALYTICS_API_IMAGE="insight-analytics-api:openapi"
    echo "::group::build analytics-api ($ANALYTICS_API_IMAGE)"
    build=(docker buildx build --load -t "$ANALYTICS_API_IMAGE"
        -f src/backend/services/analytics-api/Dockerfile src/backend)
    [ -n "${BUILD_CACHE_FROM:-}" ] && build+=(--cache-from "$BUILD_CACHE_FROM")
    "${build[@]}"
    echo "::endgroup::"
else
    echo "using provided analytics-api image: $ANALYTICS_API_IMAGE"
fi

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "::group::start MariaDB + analytics-api"
"${COMPOSE[@]}" up -d
echo "::endgroup::"

echo "waiting for analytics-api /health on :$PORT ..."
ok=
for _ in $(seq 1 90); do
    if curl -fsS -H "X-Insight-Tenant-Id: $TENANT" "http://localhost:${PORT}/health" >/dev/null 2>&1; then
        ok=1
        break
    fi
    sleep 2
done
if [ -z "$ok" ]; then
    echo "analytics-api did not become healthy" >&2
    "${COMPOSE[@]}" logs analytics-api | tail -80 >&2
    exit 1
fi

# URL mode: openapi_spec.py hits GET /openapi.json on this instance.
export ANALYTICS_API_URL="http://localhost:${PORT}"
export ANALYTICS_TENANT_ID="$TENANT"
set +e
run_py
rc=$?
set -e

if [ "$MODE" = "check" ] && [ "$rc" -ne 0 ]; then
    echo "::error::OpenAPI spec drift — docs/components/backend/analytics-api/openapi.json is stale. Run: bash scripts/ci/openapi_spec.sh update"
fi
exit "$rc"
