"""Metric-coverage gate: every metric_key the catalog exposes has its value tested.

Cross-checks, **by metric_key**, the metric universe — read over HTTP from a
running analytics-api (`POST /v1/catalog/get_metrics`: the enabled product
metric_keys, each a `<storage_table>.<column>` seeded by the analytics-api
migrations) — against the metric_keys whose VALUE the tests assert
(`find: {metric_key: …}` paired with `equal`/`assert` in the same rule). Binary
verdict per metric_key:

  • value-asserted by a test       → PASS
  • skip-listed (SKIP_LIST below)   → PASS (baseline)
  • neither                         → FAIL  (a number nobody validates)

Catalog keys are dotted (`collab_bullet_rows.m365_emails_sent`); a test asserts
the bare response key (`m365_emails_sent`). The column suffix is unique across
the catalog, so we map bare→dotted by suffix (a future collision raises — see
`CoverageReport.__post_init__`).

The skip list is the accepted baseline — inline `SKIP_LIST` (single source of
truth, no side-car file). Kept honest: a STALE entry (key no longer in the
catalog) or a REDUNDANT one (now value-tested) also fails. PASS iff no FAILs.

This module never spawns analytics-api — it reads the universe over HTTP only.
Entry point: `scripts/ci/metric_coverage.sh` (a step in the E2E — Bronze to API
workflow) boots MariaDB + analytics-api and runs this with `ANALYTICS_API_URL`
set (host needs only pyyaml + httpx). Ad hoc:
`ANALYTICS_API_URL=http://… python3 lib/metric_coverage.py [--md]`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# The tenant header the API requires (mirrors lib.config.TENANT_HEADER). Any
# non-nil tenant resolves the middleware; the catalog rows are tenant-NULL
# (global), so `get_metrics` returns them for any resolved tenant.
TENANT_HEADER = "X-Insight-Tenant-Id"
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# lib/metric_coverage.py -> lib/ -> e2e/
_E2E_ROOT = Path(__file__).resolve().parents[1]
METRICS_DIR = _E2E_ROOT / "metrics"
_WHERE = "SKIP_LIST in lib/metric_coverage.py"

# ── SKIP LIST (single source of truth) ───────────────────────────────────────
# Catalog metric_keys (`<table>.<column>`) intentionally NOT value-tested — the
# accepted baseline. Each `(metric_key, reason)`. A served metric_key that is
# neither value-asserted by a test nor listed here FAILS the gate. When a test
# starts asserting one, DELETE its row (a now-tested skip fails the gate).
# "reachable: …" entries are the actionable backlog (fixtures exist).
SKIP_LIST: list[tuple[str, str]] = [
    # ai_bullet_rows.*
    ("ai_bullet_rows.active_ai_members", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.ai_loc_share2", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cc_active", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cc_cost", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cc_lines", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cc_overage", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cc_sessions", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cc_tool_accept", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cc_tool_acceptance", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.chatgpt", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.chatgpt_active", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.claude_web", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.codex_active", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.codex_lines", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.codex_sessions", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cursor_acceptance", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cursor_active", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cursor_agents", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cursor_completions", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.cursor_lines", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.prs_total", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.prs_with_cc", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    ("ai_bullet_rows.team_ai_loc", "needs cursor / claude_code / chatgpt_team bronze fixtures."),
    # code_quality_bullet_rows.*
    ("code_quality_bullet_rows.build_success", "needs bitbucket / CI bronze fixtures."),
    ("code_quality_bullet_rows.pr_cycle_time", "needs bitbucket / CI bronze fixtures."),
    ("code_quality_bullet_rows.prs_per_dev", "needs bitbucket / CI bronze fixtures."),
    # collab_bullet_rows.*
    ("collab_bullet_rows.slack_active_days", "needs a Slack connector (no rig fixtures)."),
    ("collab_bullet_rows.slack_channel_posts", "needs a Slack connector (no rig fixtures)."),
    ("collab_bullet_rows.slack_messages_sent", "needs a Slack connector (no rig fixtures)."),
    ("collab_bullet_rows.slack_msgs_per_active_day", "needs a Slack connector (no rig fixtures)."),
    ("collab_bullet_rows.zoom_meeting_hours", "reachable: zoom fixtures exist — test pending."),
    ("collab_bullet_rows.zoom_meetings", "reachable: zoom fixtures exist — test pending."),
    # crm_bullet_rows.*
    ("crm_bullet_rows.avg_deal_size", "needs hubspot bronze fixtures."),
    ("crm_bullet_rows.calls", "needs hubspot bronze fixtures."),
    ("crm_bullet_rows.comms_per_won", "needs hubspot bronze fixtures."),
    ("crm_bullet_rows.cycle_days", "needs hubspot bronze fixtures."),
    ("crm_bullet_rows.deals_opened", "needs hubspot bronze fixtures."),
    ("crm_bullet_rows.emails", "needs hubspot bronze fixtures."),
    ("crm_bullet_rows.meetings", "needs hubspot bronze fixtures."),
    ("crm_bullet_rows.win_rate", "needs hubspot bronze fixtures."),
    # git_bullet_rows.*
    ("git_bullet_rows.clean_loc", "needs bitbucket bronze fixtures."),
    ("git_bullet_rows.commits", "needs bitbucket bronze fixtures."),
    ("git_bullet_rows.commits_per_active_day", "needs bitbucket bronze fixtures."),
    ("git_bullet_rows.lines_per_commit", "needs bitbucket bronze fixtures."),
    ("git_bullet_rows.merge_rate", "needs bitbucket bronze fixtures."),
    ("git_bullet_rows.pr_size", "needs bitbucket bronze fixtures."),
    ("git_bullet_rows.prs_created", "needs bitbucket bronze fixtures."),
    # ic_kpis.*
    ("ic_kpis.ai_loc_share_pct", "composite heatmap KPI — needs cursor + bitbucket fixtures alongside jira/m365."),
    ("ic_kpis.ai_sessions", "composite heatmap KPI — needs cursor + bitbucket fixtures alongside jira/m365."),
    ("ic_kpis.bugs_fixed", "composite heatmap KPI — needs cursor + bitbucket fixtures alongside jira/m365."),
    ("ic_kpis.focus_time_pct", "composite heatmap KPI — needs cursor + bitbucket fixtures alongside jira/m365."),
    ("ic_kpis.pr_cycle_time_h", "composite heatmap KPI — needs cursor + bitbucket fixtures alongside jira/m365."),
    ("ic_kpis.prs_merged", "composite heatmap KPI — needs cursor + bitbucket fixtures alongside jira/m365."),
    ("ic_kpis.tasks_closed", "composite heatmap KPI — needs cursor + bitbucket fixtures alongside jira/m365."),
    # support_bullet_rows.*
    ("support_bullet_rows.support_active", "needs zendesk bronze fixtures."),
    ("support_bullet_rows.support_csat", "needs zendesk bronze fixtures."),
    ("support_bullet_rows.support_kb", "needs zendesk bronze fixtures."),
    ("support_bullet_rows.support_private_comments", "needs zendesk bronze fixtures."),
    ("support_bullet_rows.support_public_comments", "needs zendesk bronze fixtures."),
    ("support_bullet_rows.support_solved", "needs zendesk bronze fixtures."),
    ("support_bullet_rows.support_updates", "needs zendesk bronze fixtures."),
    # task_delivery_bullet_rows.*  (reachable — jira fixtures exist; drain this backlog)
    ("task_delivery_bullet_rows.avg_slip", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.estimation_accuracy", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.flow_efficiency", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.mean_time_to_resolution", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.on_time_delivery", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.overrun_ratio", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.pickup_time", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.scope_completion", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.scope_creep", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.stale_in_progress", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.task_dev_time", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.task_reopen_rate", "reachable: jira fixtures exist — test pending."),
    ("task_delivery_bullet_rows.worklog_logging_accuracy", "reachable: jira fixtures exist — test pending."),
    # wiki_bullet_rows.*
    ("wiki_bullet_rows.wiki_active_authors", "needs confluence / outline bronze fixtures."),
    ("wiki_bullet_rows.wiki_comments", "needs confluence / outline bronze fixtures."),
    ("wiki_bullet_rows.wiki_edits", "needs confluence / outline bronze fixtures."),
    ("wiki_bullet_rows.wiki_pages_created", "needs confluence / outline bronze fixtures."),
]


def suffix(metric_key: str) -> str:
    """The `<column>` part of a `<table>.<column>` catalog key (or the bare key)."""
    return metric_key.split(".", 1)[-1]


def skip_index() -> dict[str, str]:
    """`{metric_key: reason}` from `SKIP_LIST`. Raises on a duplicate key."""
    out: dict[str, str] = {}
    for key, reason in SKIP_LIST:
        if key in out:
            raise ValueError(f"duplicate metric_key in SKIP_LIST: {key}")
        out[key] = reason
    return out


def universe_from_url(base_url: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, str]:
    """`{metric_key: label}` from `POST {base_url}/v1/catalog/get_metrics` — the
    enabled product metric_keys (dotted `<table>.<column>`).

    Sourced from the API (not a raw `metric_catalog` SELECT) so the gate checks
    the contract consumers see; the endpoint already returns exactly the enabled
    catalog rows. Response shape: `{"metrics": [{"metric_key", "label", ...}]}`.
    """
    import httpx  # local import: keeps the pure logic importable without httpx

    with httpx.Client(base_url=base_url, timeout=30.0, headers={TENANT_HEADER: tenant_id}) as c:
        resp = c.post("/v1/catalog/get_metrics", json={})
        resp.raise_for_status()
        body = resp.json()
    metrics = body.get("metrics", []) if isinstance(body, dict) else []
    return {str(m["metric_key"]): str(m.get("label", "")) for m in metrics}


def asserted_keys_from_tests(metrics_dir: Path = METRICS_DIR) -> dict[str, set[str]]:
    """`{bare_metric_key: {test files}}` — keys whose VALUE a test checks.

    A key counts only when a `find: {metric_key: …}` selector is paired with an
    `equal` or `assert` in the SAME expect rule (i.e. the value is validated, not
    merely selected). Plain `safe_load` — a metric_key is always a literal.
    """
    out: dict[str, set[str]] = {}
    for path in sorted(metrics_dir.glob("*.test.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for case in doc.get("cases") or []:
            for rule in case.get("expect") or []:
                mk = (rule.get("find") or {}).get("metric_key")
                if mk and ("equal" in rule or "assert" in rule):
                    out.setdefault(str(mk), set()).add(path.name)
    return out


@dataclass
class CoverageReport:
    universe: dict[str, str]  # metric_key (dotted) -> label
    asserted: dict[str, set[str]]  # bare metric_key -> {files}
    skips: dict[str, str]  # metric_key (dotted) -> reason

    # Derived sets (dotted metric_keys unless noted), populated in __post_init__.
    covered: set[str] = field(default_factory=set)  # PASS (value-tested)
    skipped_active: set[str] = field(default_factory=set)  # PASS (baseline)
    uncovered: set[str] = field(default_factory=set)  # FAIL (a number nobody validates)
    redundant_skips: set[str] = field(default_factory=set)  # FAIL (skip-listed AND tested)
    stale_skips: set[str] = field(default_factory=set)  # FAIL (skip for a non-existent key)
    unknown_asserted: set[str] = field(default_factory=set)  # FAIL (bare key, no catalog match)

    def __post_init__(self) -> None:
        # Map the catalog's dotted keys by their unique column suffix so a test's
        # bare assertion key resolves to one catalog key.
        by_suffix: dict[str, str] = {}
        for k in self.universe:
            s = suffix(k)
            if s in by_suffix:
                raise ValueError(
                    f"catalog suffix collision {s!r} ({by_suffix[s]} vs {k}) — "
                    f"bare→dotted suffix mapping is unsafe; scope by metric_id instead."
                )
            by_suffix[s] = k

        for bare in self.asserted:
            full = by_suffix.get(bare)
            (self.covered if full else self.unknown_asserted).add(full or bare)

        u, s = set(self.universe), set(self.skips)
        self.redundant_skips = s & self.covered
        self.stale_skips = s - u
        self.skipped_active = (s & u) - self.covered
        self.uncovered = u - self.covered - s

    @property
    def passed(self) -> bool:
        return not (
            self.uncovered or self.redundant_skips or self.stale_skips or self.unknown_asserted
        )

    def files_for(self, full_key: str) -> set[str]:
        return self.asserted.get(suffix(full_key), set())


def build_report(universe: dict[str, str], metrics_dir: Path = METRICS_DIR) -> CoverageReport:
    """Assemble the report. `universe` comes from `universe_from_url` (the catalog
    metric_keys the API serves); asserted + skips are local to the rig."""
    return CoverageReport(
        universe=universe,
        asserted=asserted_keys_from_tests(metrics_dir),
        skips=skip_index(),
    )


def gate_violations(r: CoverageReport) -> list[str]:
    """Human-readable FAIL reasons. Empty list == gate PASS."""
    out: list[str] = []
    for k in sorted(r.uncovered):
        out.append(
            f"FAIL `{k}` — served by the catalog but no test asserts its value and it is "
            f"not skip-listed. Add a `find: {{metric_key: {suffix(k)}}}` + `equal`/`assert`, "
            f"or add it to {_WHERE}."
        )
    for k in sorted(r.redundant_skips):
        files = ", ".join(sorted(r.files_for(k)))
        out.append(
            f"FAIL `{k}` — skip-listed but now value-tested by [{files}]. Remove its entry "
            f"from {_WHERE}."
        )
    for k in sorted(r.stale_skips):
        out.append(
            f"FAIL `{k}` — skip-listed but no longer a catalog metric_key (removed/renamed). "
            f"Remove it from {_WHERE}."
        )
    for bare in sorted(r.unknown_asserted):
        files = ", ".join(sorted(r.asserted[bare]))
        out.append(
            f"FAIL `{bare}` — asserted by [{files}] but is not a catalog metric_key (typo, or "
            f"an unseeded key that matches 0 rows)."
        )
    return out


# Friendly vector names for the storage tables (display only).
_VECTOR_NAMES = {
    "collab_bullet_rows": "Collaboration",
    "task_delivery_bullet_rows": "Task Delivery",
    "ai_bullet_rows": "AI Adoption",
    "git_bullet_rows": "Git Activity",
    "code_quality_bullet_rows": "Code Quality",
    "crm_bullet_rows": "CRM / Sales",
    "support_bullet_rows": "Support",
    "wiki_bullet_rows": "Wiki / Knowledge",
    "ic_kpis": "IC KPIs (heatmap)",
}


def _vector(metric_key: str) -> str:
    return metric_key.split(".", 1)[0]


def _vector_name(table: str) -> str:
    return _VECTOR_NAMES.get(table, table)


def _by_table(keys) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(_vector(k), []).append(k)
    return groups


def _pct(n: int, d: int) -> str:
    return f"{round(100 * n / d)}%" if d else "—"


def _is_reachable(reason: str) -> bool:
    """A skip whose fixtures already exist — the actionable backlog."""
    return reason.lower().startswith("reachable")


def render_text(r: CoverageReport) -> str:
    cov, skp, tot = len(r.covered), len(r.skipped_active), len(r.universe)
    backlog = [k for k in r.skipped_active if _is_reachable(r.skips[k])]
    lines = [
        f"Metric coverage (by metric_key): {'PASS' if r.passed else 'FAIL'}  "
        f"({cov}/{tot} validated {_pct(cov, tot)}, {skp} skipped [{len(backlog)} reachable], "
        f"{len(r.uncovered)} missing)",
    ]
    for t, keys in sorted(_by_table(r.universe).items()):
        c = sum(1 for k in keys if k in r.covered)
        lines.append(f"  {_vector_name(t):20} {c}/{len(keys)}")
    for v in gate_violations(r):
        lines.append(f"  ✗ {v}")
    return "\n".join(lines)


def render_markdown(r: CoverageReport) -> str:
    """Markdown report: a per-vector summary + the reachable backlog up top, then
    the full per-key detail (collapsed), then a skip-list-hygiene footer."""
    cov, skp, tot, miss = (
        len(r.covered), len(r.skipped_active), len(r.universe), len(r.uncovered),
    )
    out = [
        "# Metric coverage — by metric_key",
        "",
        f"**Gate: {'✅ PASS' if r.passed else '❌ FAIL'}.** "
        f"{cov}/{tot} numbers validated ({_pct(cov, tot)}) · {skp} baseline-skipped · "
        f"**{miss} missing**.",
    ]

    # ── Per-vector summary ───────────────────────────────────────────────────
    tables = _by_table(r.universe)
    out += ["", "## Coverage by vector", "",
            "| vector | tested | skipped | missing | coverage |",
            "|---|--:|--:|--:|--:|"]
    for t in sorted(tables, key=lambda x: (-sum(1 for k in tables[x] if k in r.covered), x)):
        keys = tables[t]
        c = sum(1 for k in keys if k in r.covered)
        s = sum(1 for k in keys if k in r.skipped_active)
        m = sum(1 for k in keys if k in r.uncovered)
        out.append(f"| {_vector_name(t)} | {c} | {s} | {m} | {_pct(c, len(keys))} |")
    out.append(f"| **Total** | **{cov}** | **{skp}** | **{miss}** | **{_pct(cov, tot)}** |")

    # ── Reachable backlog (fixtures exist — just write the assertion) ─────────
    backlog = sorted(k for k in r.skipped_active if _is_reachable(r.skips[k]))
    if backlog:
        out += ["", f"## Reachable now — backlog ({len(backlog)})",
                "_Fixtures already exist; each just needs a `find:`+`equal` assertion in a test._",
                ""]
        for k in backlog:
            out.append(f"- **{r.universe[k] or suffix(k)}** — `{suffix(k)}` ({_vector_name(_vector(k))})")

    # ── Full per-key detail (collapsed) ──────────────────────────────────────
    out += ["", "<details><summary>Per-key detail (all "
            f"{tot})</summary>", ""]
    for t in sorted(tables):
        keys = sorted(tables[t])
        c = sum(1 for k in keys if k in r.covered)
        out += ["", f"### {_vector_name(t)} (`{t}`) — {c}/{len(keys)}", "",
                "| verdict | metric | key | detail |", "|---|---|---|---|"]
        for k in keys:
            col, label = suffix(k), (r.universe[k] or suffix(k))
            if k in r.uncovered:
                out.append(f"| ❌ MISSING | {label} | `{col}` | no value assertion, not skip-listed |")
            elif k in r.covered:
                out.append(f"| ✅ tested | {label} | `{col}` | {', '.join(sorted(r.files_for(k)))} |")
            else:
                out.append(f"| ⏭️ baseline | {label} | `{col}` | {r.skips[k]} |")
    out += ["", "</details>"]

    # ── Skip-list hygiene (these also fail the gate) ─────────────────────────
    hygiene: list[str] = []
    for k in sorted(r.redundant_skips):
        hygiene.append(f"- `{k}` skip-listed but now tested by [{', '.join(sorted(r.files_for(k)))}]; remove from SKIP_LIST.")
    for k in sorted(r.stale_skips):
        hygiene.append(f"- `{k}` skip-listed but no longer in the catalog; remove from SKIP_LIST.")
    for bare in sorted(r.unknown_asserted):
        hygiene.append(f"- `{bare}` asserted by [{', '.join(sorted(r.asserted[bare]))}] is not a catalog metric_key (typo/unseeded).")
    if hygiene:
        out += ["", "## Skip-list issues (also fail the gate)", *hygiene]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI: print the coverage table/report; exit non-zero on any gate failure.

    `--md` prints the markdown status table (default: the plain-text report).
    Reads the universe over HTTP from a running analytics-api: set
    `ANALYTICS_API_URL` (and optionally `ANALYTICS_TENANT_ID`). The standalone
    script `scripts/ci/metric_coverage.sh` sets these for you. This module never
    spawns analytics-api itself.
    """
    args = argv if argv is not None else sys.argv[1:]
    url = os.environ.get("ANALYTICS_API_URL")
    if not url:
        print(
            "metric coverage: set ANALYTICS_API_URL to a running analytics-api, then "
            "re-run. The gate `scripts/ci/metric_coverage.sh` does this for you.",
            file=sys.stderr,
        )
        return 2
    universe = universe_from_url(url, os.environ.get("ANALYTICS_TENANT_ID", DEFAULT_TENANT_ID))

    report = build_report(universe)
    if not report.universe:
        print(
            "metric coverage: POST /v1/catalog/get_metrics returned no metrics — the "
            "catalog isn't seeded. Check analytics-api startup / migrations.",
            file=sys.stderr,
        )
        return 1
    as_md = "--md" in args
    print(render_markdown(report) if as_md else render_text(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
