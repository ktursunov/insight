#!/usr/bin/env python3
"""Emit the per-language CI matrix of the components a PR touches, as JSON, for
the GitHub Actions `changes` job. Sources the component↔path map and the
per-component collection params from components.py (the shared registry) — so no
globs are duplicated in YAML and there is one source of truth. Runs no tests.

Usage: python3 scripts/ci/changed.py
Output: {"rust": [<entry>...], "dotnet": [...], "python": [...], "lint_backend": bool}
        where each <entry> carries the fields its producer job needs to collect
        coverage (rust: package/all_features, dotnet: solution, python: cov_package),
        and lint_backend says whether rust-lint must run for the src/backend workspace.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from components import ROOT, COMPARE_BRANCH, COMPONENTS, component_for

# Workspace-level Rust files under this root (Cargo.toml/lock, clippy.toml,
# rustfmt.toml, a shared crate) map to no single component. A change to one must
# still run rust-lint — it lints the whole src/backend workspace (clippy.toml
# steers clippy directly) — else such a change would silently bypass linting.
BACKEND_RUST_ROOT = "src/backend"
_SHARED_RUST_SUFFIXES = (".rs", ".toml", ".lock")


def _matrix_entry(comp: dict) -> dict:
    """The fields a producer CI job needs to collect coverage for one component."""
    entry = {"name": comp["name"], "root": comp["root"]}
    if comp["lang"] == "rust":
        entry["package"] = comp.get("package", comp["name"])
        entry["all_features"] = comp.get("all_features", True)
    elif comp["lang"] == "dotnet":
        entry["solution"] = comp.get("solution", "")
    elif comp["lang"] == "python":
        entry["cov_package"] = comp.get("cov_package", "")
    return entry


def changed_components(compare_branch: str, components: list[dict]) -> dict[str, object]:
    """Map the diff (vs the merge-base with compare_branch) to changed components,
    grouped by language, as rich CI-matrix entries, plus a `lint_backend` flag.
    CI runs one producer job per entry, so only the components a PR touches are
    built; coverage stays strictly per-component (no sibling fanout)."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{compare_branch}...HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    changed: set[str] = set()
    backend_shared = False
    for line in out.splitlines():
        path = line.strip()
        if not path:
            continue
        name = component_for(path, components)
        if name:
            changed.add(name)
        elif path.startswith(BACKEND_RUST_ROOT + "/") and path.endswith(_SHARED_RUST_SUFFIXES):
            backend_shared = True  # workspace-level Rust file (clippy.toml, Cargo.*, …)
    result: dict[str, object] = {lang: [] for lang in ("rust", "dotnet", "python")}
    for comp in components:  # registry order → deterministic matrix
        if comp["name"] in changed:
            result[comp["lang"]].append(_matrix_entry(comp))
    # rust-lint lints the whole src/backend workspace, so run it when any backend
    # Rust *source* changed OR a workspace-level Rust file did. Shared changes
    # trigger LINT only — never a coverage fanout: they touch no crate's source
    # (nothing new to cover), and fanning out would break per-component isolation.
    backend_rust = {c["name"] for c in components
                    if c["lang"] == "rust" and c.get("root") == BACKEND_RUST_ROOT}
    result["lint_backend"] = backend_shared or bool(changed & backend_rust)
    return result


def all_components(components: list[dict]) -> dict[str, object]:
    """Emit EVERY component as a matrix entry — for a manual full/baseline run
    (workflow_dispatch with full=true), independent of the diff."""
    result: dict[str, object] = {lang: [] for lang in ("rust", "dotnet", "python")}
    for comp in components:
        result[comp["lang"]].append(_matrix_entry(comp))
    result["lint_backend"] = any(
        c["lang"] == "rust" and c.get("root") == BACKEND_RUST_ROOT for c in components)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit the CI component matrix as JSON.")
    ap.add_argument("--all", action="store_true",
                    help="emit ALL components, ignoring the diff (manual full run)")
    args = ap.parse_args()
    matrix = all_components(COMPONENTS) if args.all else changed_components(COMPARE_BRANCH, COMPONENTS)
    print(json.dumps(matrix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
