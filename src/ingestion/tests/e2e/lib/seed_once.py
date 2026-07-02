"""Seed-once world builder: populate the whole stack from ALL fixtures at once.

Called once per session (see conftest.build_world fixture). Every fixture's
bronze rows are namespaced (lib.namespace) so they can coexist without
ReplacingMergeTree collapse or cross-fixture aggregate/join bleed, then seeded
together:

    seed all namespaced bronze
      -> dbt build staging (union of every fixture's touched models)
      -> connector enrich (once, over all seeded source_ids)
      -> dbt build silver (union + ephemeral enrich-fed targets)
      -> reapply gold-view migrations ONCE  (realign views to real silver)
      -> refresh refreshable MVs ONCE

`ch_bootstrap` already applied all migrations (so `identity`/`person` exist for
dbt to write into) with gold views bound to the silver placeholders. dbt then
drops those placeholders and materialises real silver, so the views must be
rebound once — that is the single `reapply_migrations` below. It replaces the
old per-fixture reapply (≈40 views × every fixture → once per session); the
subsequent per-test path does no DDL at all.
"""

from __future__ import annotations

import logging

from lib import namespace
from lib.ch_seeder import CHSeeder
from lib.dbt_runner import DbtRunner
from lib.enrich import EnrichRunner
from lib.fixture_loader import TestYaml
from lib.migration_applier import reapply_migrations as apply_gold_migrations
from lib.migration_applier import refresh_intermediates
from lib.worker import WorkerContext

LOG = logging.getLogger("e2e.seed_once")


def merge_namespaced_bronze(fixtures: list[TestYaml]) -> dict[str, list[dict]]:
    """Union every fixture's bronze, each record namespaced by its fixture token.

    Returns a single `table_fqn -> [records]` map ready for one `seed_bronze`.
    """
    merged: dict[str, list[dict]] = {}
    for ty in fixtures:
        token = namespace.token_for(ty.name)
        for tbl, rows in namespace.namespace_bronze(ty.bronze, token).items():
            merged.setdefault(tbl, []).extend(rows)
    return merged


def build_world(
    *,
    seeder: CHSeeder,
    dbt_runner: DbtRunner,
    enrich_runner: EnrichRunner,
    fixtures: list[TestYaml],
    worker_ctx: WorkerContext,
) -> None:
    """Seed all fixtures and build the stack once, in prod order."""
    merged = merge_namespaced_bronze(fixtures)
    if not merged:
        LOG.warning("no bronze across %d fixtures — nothing to seed", len(fixtures))
        return

    total_rows = sum(len(r) for r in merged.values())
    LOG.info(
        "seed-once: %d fixtures, %d bronze tables, %d rows",
        len(fixtures), len(merged), total_rows,
    )

    # 1. Seed the merged bronze (seed_bronze truncates each table then inserts).
    seeder.seed_bronze(merged)
    touched = {(fqn.split(".", 1)[0], fqn.split(".", 1)[1]) for fqn in merged}

    # 2. Staging models fed by the seeded bronze (union across fixtures).
    staging, silver = dbt_runner.derive_selectors(touched)
    if staging:
        for st in staging:
            seeder.ledger.record("staging", st)
        dbt_runner.build(" ".join(f"+{m}" for m in staging), worker_ctx=worker_ctx)

    # 3. Connector enrich steps (once, over every seeded source_id).
    touched_schemas = {schema for schema, _ in touched}
    ran_enrich_steps = []
    for step in enrich_runner.steps_for(touched_schemas):
        source_ids = enrich_runner.discover_source_ids(step, touched)
        if not source_ids:
            continue
        for schema, table in dbt_runner.enrich_output_tables(step.name):
            seeder.truncate_table(schema, table)
        enrich_runner.run(step, source_ids)
        ran_enrich_steps.append(step)

    # 4. Silver class models: those fed by seeded bronze + ephemeral enrich-fed targets.
    silver_set = set(silver)
    for step in ran_enrich_steps:
        silver_set.update(dbt_runner.ephemeral_silver_targets(step.name))
    if silver_set:
        for cls in silver_set:
            seeder.ledger.record("silver", cls)
        dbt_runner.build(" ".join(sorted(silver_set)), worker_ctx=worker_ctx)

    # 5. Realign gold views to the now-real, populated silver — ONCE. (ch_bootstrap
    #    created them against the placeholders that dbt just dropped+rebuilt.)
    apply_gold_migrations(seeder.cfg)

    # 6. Refresh refreshable MVs ONCE.
    refresh_intermediates(seeder.cfg)
    LOG.info("seed-once world built: %d staging, %d silver, %d enrich steps",
             len(staging), len(silver_set), len(ran_enrich_steps))
