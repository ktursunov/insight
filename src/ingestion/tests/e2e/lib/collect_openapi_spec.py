#!/usr/bin/env python3
"""Snapshot the live OpenAPI spec from a running analytics-api.

NOT a pytest test — a plain script. It is run (via subprocess) by conftest's
`analytics_api` fixture while the suite's analytics-api is still up, so the CI
spec-drift gate can analyse a plain file with no second app boot. Writes, into
``--out-dir``:

  • openapi.live.json     ← GET /openapi.json   (openapi-spec-drift gate)

(The endpoint-coverage ledger, observed_endpoints.json, is written separately by
conftest.pytest_sessionfinish from the in-process httpx-hook recorder.)

Standalone:
    python3 lib/collect_openapi_spec.py \
        --url http://127.0.0.1:8081 --out-dir .artifacts
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def collect(base_url: str, out_dir: str | Path) -> None:
    """Fetch the live spec and write it to ``out_dir``.

    Fail-fast (raises) if the response carries no paths — a missing/empty
    artifact would otherwise surface only as a confusing downstream gate failure.
    """
    import httpx  # local import: keeps this importable without httpx

    # /openapi.json is public — no X-Insight-Tenant-Id header needed.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        spec = c.get("/openapi.json")
        spec.raise_for_status()

    spec_doc = spec.json()
    if not (isinstance(spec_doc, dict) and spec_doc.get("paths")):
        raise SystemExit(f"collect: GET {base_url}/openapi.json returned no paths")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "openapi.live.json").write_text(json.dumps(spec_doc, indent=2) + "\n", encoding="utf-8")
    print(f"collected openapi.live.json ({len(spec_doc['paths'])} paths) -> {out}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Snapshot the live OpenAPI spec from a running analytics-api."
    )
    p.add_argument("--url", required=True, help="analytics-api base URL")
    p.add_argument("--out-dir", required=True, help="directory to write the artifact into")
    args = p.parse_args(argv)
    collect(args.url, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
