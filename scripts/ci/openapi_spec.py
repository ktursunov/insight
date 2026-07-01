#!/usr/bin/env python3
"""Generate the committed analytics-api OpenAPI spec, and canonicalize a spec file.

The live contract is the in-process registry served at ``GET /openapi.json``
(``src/backend/services/analytics-api/src/api/mod.rs``). This script owns the two
Python-side jobs; the drift-check GATE lives in the sibling ``openapi_spec.sh``,
which calls ``normalize`` here and diffs:

    python3 scripts/ci/openapi_spec.py update           # rewrite the committed doc from the live spec
    python3 scripts/ci/openapi_spec.py normalize <file>  # print the canonical form of a spec file (used by the gate)

Live-spec source for ``update`` (precedence order):
  --url / $ANALYTICS_API_URL   fetch a running analytics-api (needs httpx)
  --live-file <path>           a saved GET /openapi.json
  (default)                    the openapi.live.json the e2e run collects into
                               src/ingestion/tests/e2e/.artifacts/ — run
                               ``./e2e.sh test`` first (CI passes the downloaded
                               artifact explicitly).

Pure stdlib unless fetching (--url). Paths default relative to the repo root, so
it runs from any working directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# scripts/ci/openapi_spec.py -> scripts/ci -> scripts -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
# Fixed repo artifact paths (code constants, not operator-tunable input) —
# overridable with --file / --live-file.
DEFAULT_SPEC_FILE = _REPO_ROOT / "docs/components/backend/analytics-api/openapi.json"
DEFAULT_LIVE_FILE = _REPO_ROOT / "src/ingestion/tests/e2e/.artifacts/openapi.live.json"
SPEC_ROUTE = "/openapi.json"


def normalize(doc: object) -> str:
    """Canonical on-disk form: sorted keys, 2-space indent, trailing newline.

    Sorting keys makes the form independent of the registry's emission order (the
    toolkit iterates a ``DashMap``), so ``update`` produces a minimal, review-
    friendly diff and the ``openapi_spec.sh`` gate compares stably run-to-run.
    """
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def fetch_live_spec(base_url: str, tenant_id: str | None) -> object:
    import httpx  # local import: keep the file-only path importable without httpx

    headers: dict[str, str] = {}
    if tenant_id:
        # /openapi.json is public, but sending the header is harmless and keeps
        # the call uniform with the rest of the rig.
        headers["X-Insight-Tenant-Id"] = tenant_id
    with httpx.Client(base_url=base_url, timeout=30.0, headers=headers) as c:
        r = c.get(SPEC_ROUTE)
        r.raise_for_status()
        return r.json()


def _load_live(args: argparse.Namespace) -> tuple[str, str] | None:
    """Return (normalized live spec, human-readable source), or None on a missing
    file (after printing an error)."""
    url = args.url or os.environ.get("ANALYTICS_API_URL")
    if url:
        tenant = args.tenant or os.environ.get("ANALYTICS_TENANT_ID")
        return normalize(fetch_live_spec(url, tenant)), f"{url}{SPEC_ROUTE}"
    live_path = Path(args.live_file)
    if not live_path.exists():
        print(
            f"ERROR: {live_path} not found — run `./e2e.sh test` first (it collects "
            f"the live spec), or pass --url / $ANALYTICS_API_URL to fetch a running API",
            file=sys.stderr,
        )
        return None
    return normalize(json.loads(live_path.read_text(encoding="utf-8"))), str(live_path)


def _cmd_update(args: argparse.Namespace) -> int:
    loaded = _load_live(args)
    if loaded is None:
        return 2
    live, source = loaded
    path = Path(args.file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(live, encoding="utf-8")
    print(f"wrote {path} ({len(live.splitlines())} lines) from {source}")
    return 0


def _cmd_normalize(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2
    sys.stdout.write(normalize(json.loads(path.read_text(encoding="utf-8"))))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Generate / canonicalize the analytics-api OpenAPI spec "
        "(the drift-check gate is scripts/ci/openapi_spec.sh)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="rewrite the committed doc from the live spec")
    up.add_argument("--url", help="fetch a running analytics-api (default: $ANALYTICS_API_URL)")
    up.add_argument(
        "--live-file",
        default=str(DEFAULT_LIVE_FILE),
        help="saved GET /openapi.json to read when not fetching "
        "(default: the e2e-collected artifact)",
    )
    up.add_argument("--file", default=str(DEFAULT_SPEC_FILE), help="committed spec path")
    up.add_argument(
        "--tenant",
        help="X-Insight-Tenant-Id header (default: $ANALYTICS_TENANT_ID; "
        "optional — the route is public)",
    )
    up.set_defaults(func=_cmd_update)

    nm = sub.add_parser("normalize", help="print the canonical form of a spec file to stdout")
    nm.add_argument("file", help="path to a JSON OpenAPI document")
    nm.set_defaults(func=_cmd_normalize)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
