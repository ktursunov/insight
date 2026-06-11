"""
ClickHouse silver-layer seed — Phase 2.

Placeholder until the silver bootstrap (CREATE silver tables + apply gold
view migrations from src/ingestion/scripts/migrations/*.sql) + the
per-domain row generators land in a follow-up commit. See
/Users/antonz/Sources/cf/SEED_DATA_FORMAT.md sections 4-5 for the plan.
"""

from __future__ import annotations


def run() -> None:
    raise SystemExit(
        "silver: not yet implemented. Run `seed.py identity` for now."
    )
