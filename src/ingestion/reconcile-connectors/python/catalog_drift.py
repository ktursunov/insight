#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# catalog_drift.py
#
# Decide whether a connection's CURRENT syncCatalog drifts from a freshly
# discovered+normalized catalog. Used by reconcile_refresh_catalog to make the
# catalog refresh idempotent: re-discover runs on every reconcile pass (so a
# stale connection catalog self-heals within one schedule interval), but the
# /connections/update PATCH only fires when something actually changed — no
# write-churn and no needless state churn when nothing moved.
#
# Inputs:
#   stdin                — the NEW normalized syncCatalog (output of
#                          normalize_catalog_to_append.py): {"streams":[...]}.
#   env CURRENT_SYNC_CATALOG — the connection's current syncCatalog JSON
#                          (the `.syncCatalog` of a connections/get response).
#
# Compares a canonical per-stream projection that captures exactly the shape
# reconcile owns: the set of stream names, each stream's jsonSchema property
# keys, and its sync config (sync/destination mode, selection, cursor). Field
# additions (the bug this whole change targets), stream add/remove, and
# sync-mode flips all surface; incidental key ordering / Airbyte-injected
# defaults do not.
#
# Output: prints "drift" or "same" to stdout. Exit 0 on success, 2 on a
# parse/IO error (caller treats an error as "fail closed" — see reconcile.sh).
# ---------------------------------------------------------------------------

import json
import os
import sys


def _project(catalog: dict) -> dict:
    """name -> canonical tuple of the parts reconcile manages."""
    out = {}
    for entry in (catalog.get("streams") or []):
        stream = entry.get("stream") or {}
        name = stream.get("name")
        if not name:
            continue
        props = sorted((stream.get("jsonSchema", {}) or {}).get("properties", {}) or {})
        cfg = entry.get("config") or {}
        cursor = cfg.get("cursorField") or []
        out[name] = (
            tuple(props),
            cfg.get("syncMode"),
            cfg.get("destinationSyncMode"),
            bool(cfg.get("selected")),
            bool(cfg.get("fieldSelectionEnabled")),
            tuple(cursor),
        )
    return out


def main() -> int:
    try:
        new_catalog = json.load(sys.stdin)
        current = json.loads(os.environ.get("CURRENT_SYNC_CATALOG", "") or "{}")
    except (json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"catalog_drift: cannot parse input: {exc}\n")
        return 2
    print("drift" if _project(new_catalog) != _project(current) else "same")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
