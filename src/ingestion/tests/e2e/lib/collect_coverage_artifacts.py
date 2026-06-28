#!/usr/bin/env python3
"""Snapshot the coverage-gate inputs from a running analytics-api.

NOT a pytest test — a plain script. It is run (via subprocess) by conftest's
`analytics_api` fixture while the suite's analytics-api is still up, so the CI
gate jobs can analyse plain files with no second app boot. Writes, into
``--out-dir``:

  • openapi.live.json     ← GET /openapi.json           (openapi-spec-drift gate)
  • catalog_metrics.json  ← POST /v1/catalog/get_metrics (metric-coverage gate)

(The endpoint-coverage ledger, observed_endpoints.json, is written separately by
conftest.pytest_sessionfinish from the in-process httpx-hook recorder.)

Standalone:
    python3 lib/collect_coverage_artifacts.py \
        --url http://127.0.0.1:8081 --out-dir .artifacts \
        --tenant 00000000-0000-0000-0000-000000000001
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def collect(base_url: str, out_dir: str | Path, tenant_id: str | None) -> None:
    """Fetch the live spec + catalog and write them to ``out_dir``.

    Fail-fast (raises) if either response is empty — a missing/empty artifact
    would otherwise surface only as a confusing downstream gate failure.
    """
    import httpx  # local import: keeps this importable without httpx

    headers: dict[str, str] = {}
    if tenant_id:
        # /openapi.json is public; the catalog read is tenant-gated.
        headers["X-Insight-Tenant-Id"] = tenant_id
    with httpx.Client(base_url=base_url, timeout=30.0, headers=headers) as c:
        spec = c.get("/openapi.json")
        spec.raise_for_status()
        catalog = c.post("/v1/catalog/get_metrics", json={})
        catalog.raise_for_status()

    spec_doc = spec.json()
    catalog_doc = catalog.json()
    if not (isinstance(spec_doc, dict) and spec_doc.get("paths")):
        raise SystemExit(f"collect: GET {base_url}/openapi.json returned no paths")
    if not (isinstance(catalog_doc, dict) and catalog_doc.get("metrics")):
        raise SystemExit(f"collect: POST {base_url}/v1/catalog/get_metrics returned no metrics")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "openapi.live.json").write_text(json.dumps(spec_doc, indent=2) + "\n", encoding="utf-8")
    (out / "catalog_metrics.json").write_text(json.dumps(catalog_doc, indent=2) + "\n", encoding="utf-8")
    print(
        f"collected openapi.live.json ({len(spec_doc['paths'])} paths) + "
        f"catalog_metrics.json ({len(catalog_doc['metrics'])} metrics) -> {out}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Snapshot coverage-gate inputs from a running analytics-api."
    )
    p.add_argument("--url", required=True, help="analytics-api base URL")
    p.add_argument("--out-dir", required=True, help="directory to write the artifacts into")
    p.add_argument("--tenant", help="X-Insight-Tenant-Id header (the catalog read is tenant-gated)")
    args = p.parse_args(argv)
    collect(args.url, args.out_dir, args.tenant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
