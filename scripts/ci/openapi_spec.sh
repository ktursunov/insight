#!/usr/bin/env bash
# Generate or drift-check the committed analytics-api OpenAPI spec — from the
# live spec the e2e run already collected (no Docker, no app boot).
#
#   ./e2e.sh test                          # collects .artifacts/openapi.live.json
#   bash scripts/ci/openapi_spec.sh update # rewrite the committed doc from it
#   bash scripts/ci/openapi_spec.sh check  # exit 2 + diff if the doc has drifted
#
# Pure file analysis — the same thing CI's openapi-spec-drift-gate does. To work
# against a running analytics-api instead (e.g. a kube port-forward), call the
# Python directly: `python3 scripts/ci/openapi_spec.py check --url <base-url>`.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

MODE="${1:?usage: openapi_spec.sh <update|check>}"
case "$MODE" in
    update) PY_MODE=write ;;
    check)  PY_MODE=check ;;
    *) echo "unknown mode: $MODE (expected: update | check)" >&2; exit 2 ;;
esac

# The live spec snapshotted by the e2e run (lib/collect_coverage_artifacts.py).
LIVE="src/ingestion/tests/e2e/.artifacts/openapi.live.json"
if [ ! -f "$LIVE" ]; then
    echo "no $LIVE — run './e2e.sh test' first (it collects the live spec)" >&2
    exit 2
fi

exec python3 scripts/ci/openapi_spec.py "$PY_MODE" --live-file "$LIVE"
