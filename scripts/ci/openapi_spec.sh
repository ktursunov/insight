#!/usr/bin/env bash
# Drift-check GATE for the committed analytics-api OpenAPI spec — the shell half
# of the split (spec generation lives in the sibling openapi_spec.py). The gate
# canonicalizes both the committed doc and a live snapshot via
# `openapi_spec.py normalize`, diffs them, and exits 2 (after printing the diff)
# when the committed doc has drifted from the live analytics-api router.
#
#   scripts/ci/openapi_spec.sh check --live-file <saved GET /openapi.json> [--file <committed doc>]
#
# The live spec is the openapi.live.json the e2e run collects (or CI downloads).
# Regenerate a stale doc with:
#   ./e2e.sh test && python3 scripts/ci/openapi_spec.py update
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PY="$HERE/openapi_spec.py"
# Fixed repo artifact path (code constant, not operator config); override --file.
SPEC_FILE="$REPO_ROOT/docs/components/backend/analytics-api/openapi.json"

usage() {
    echo "usage: $0 check --live-file <path> [--file <committed doc>]" >&2
    exit 2
}

[ "${1:-}" = "check" ] || usage
shift

LIVE_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --live-file) LIVE_FILE="${2:?--live-file needs a path}"; shift 2 ;;
        --file)      SPEC_FILE="${2:?--file needs a path}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

if [ -z "$LIVE_FILE" ]; then
    echo "ERROR: --live-file is required (a saved GET /openapi.json, e.g. .artifacts/openapi.live.json)" >&2
    exit 2
fi
if [ ! -f "$LIVE_FILE" ]; then
    echo "ERROR: $LIVE_FILE not found — run \`./e2e.sh test\` first (it collects the live spec)" >&2
    exit 2
fi
if [ ! -f "$SPEC_FILE" ]; then
    echo "ERROR: $SPEC_FILE does not exist — create it with \`python3 scripts/ci/openapi_spec.py update\`" >&2
    exit 2
fi

committed_tmp="$(mktemp)"
live_tmp="$(mktemp)"
trap 'rm -f "$committed_tmp" "$live_tmp"' EXIT

# Canonicalize both through the same Python normalizer so the comparison ignores
# the registry's key-emission order (see openapi_spec.py::normalize).
python3 "$PY" normalize "$SPEC_FILE" > "$committed_tmp"
python3 "$PY" normalize "$LIVE_FILE" > "$live_tmp"

if diff -u --label "$SPEC_FILE (committed)" --label "$LIVE_FILE (live)" "$committed_tmp" "$live_tmp"; then
    echo "OK: $SPEC_FILE matches the live spec ($LIVE_FILE)"
    exit 0
fi

echo >&2
echo "ERROR: $SPEC_FILE is STALE vs the live analytics-api router." >&2
echo "Regenerate it:  ./e2e.sh test && python3 scripts/ci/openapi_spec.py update" >&2
echo "(then commit the updated $SPEC_FILE)" >&2
exit 2
