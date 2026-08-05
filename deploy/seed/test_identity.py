"""Idempotency + roster-scope tests for `identity.seed_login_ids`.

No pytest / DB fixture in this package (deploy/seed has no existing test
harness) — a stdlib `unittest` test against a minimal fake cursor is enough to
lock two regressions:

1. Idempotency: migration 004 (`004_persons_relax_constraints.sql`) put
   `created_at` in `persons`' unique key, so `INSERT IGNORE` alone no longer
   dedupes a re-run (each insert gets a fresh `created_at`, so the unique key
   never collides). `seed_login_ids` must check for an existing row
   explicitly, per pair, before inserting.
2. Roster scope: fakeidp only defines a fixed dev-lead identity, but a
   Keycloak realm seeds the WHOLE roster (gen-realm.py pins every realm
   user's id to their own roster uuid) — `seed_login_ids` must seed a row per
   roster member under `AUTH_MODE=keycloak`, not just the dev lead.
3. Tenant scope: the seeder runs once per tenant, so the pairs must follow
   the roster HANDED to each call. The second tenant's lone caller needs a
   row of their own (without it the authenticator denies them at the
   callback), and fakeidp — which has no identity for that persona — must
   write nothing rather than repeating the dev lead's pair under a second
   tenant, which the tenant-agnostic login resolve requires to be unique.

`pymysql` isn't installed in every environment this runs in (it's only a
runtime dependency of the seed container image) — a minimal stand-in is
injected into `sys.modules` before importing `identity` so this test has no
external dependency beyond the stdlib.

Primary, always-works invocation, from anywhere:
`python3 -m unittest deploy/seed/test_identity.py -v`. `python3 -m unittest
discover -s deploy/seed` also works in most setups, but `discover` additionally
requires the start dir to be importable as a top-level package in some
Python/cwd combinations (no `__init__.py` here by design — `deploy/seed` is a
flat-module package, see pyproject.toml's `py-modules`) — if that fails in
your environment, use the primary invocation above.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from typing import Any

os.environ.setdefault("IDP_SOURCE_TYPE", "fakeidp")

# `identity`/`profiles` are flat modules (no package __init__), so importing
# them by name only works when deploy/seed is on sys.path. unittest's
# file-path invocation (`python3 -m unittest path/to/test_identity.py`) does
# NOT add the file's own directory the way `discover` does — add it
# explicitly so both invocation styles resolve `import identity` the same way.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

if "pymysql" not in sys.modules:
    _pymysql_stub = types.ModuleType("pymysql")
    _pymysql_stub.cursors = types.SimpleNamespace(Cursor=object)  # type: ignore[attr-defined]
    _pymysql_stub.connections = types.SimpleNamespace(Connection=object)  # type: ignore[attr-defined]
    _pymysql_stub.connect = lambda **_kwargs: None  # type: ignore[attr-defined]
    sys.modules["pymysql"] = _pymysql_stub

import identity  # noqa: E402 — stub/env setup above must run first
import profiles  # noqa: E402 — stub/env setup above must run first

_TENANT = "00000000-df51-5b42-9538-d2b56b7ee953"


def _roster() -> list[profiles.Person]:
    return [
        profiles.Person(
            uuid=profiles.DEV_LEAD_UUID,
            email="dev@company.nonpresent",
            team="development",
            role="lead",
            parent_uuid=profiles.CEO_UUID,
            first_name="Dev",
            last_name="Lead",
        ),
        profiles.Person(
            uuid=profiles.SALES_LEAD_UUID,
            email="sales-lead@company.nonpresent",
            team="sales",
            role="lead",
            parent_uuid=profiles.CEO_UUID,
            first_name="Sales",
            last_name="Lead",
        ),
    ]


class _FakeCursor:
    """Tracks which (tenant, person_id, external_id) rows have been inserted.

    `seed_login_ids` runs one SELECT-then-maybe-INSERT per roster pair; this
    fake extracts the identifying elements from each statement's params (they
    sit at different positions in the SELECT vs. INSERT param tuples — see
    `seed_login_ids`' `exists_sql`/`insert_sql`) so it can answer `fetchone()`
    per row, not just a single global flag.

    The tenant is part of the key because the real existence check scopes by
    it: one cursor sees calls for more than one tenant, and a key without it
    would report the second tenant's row as already present.
    """

    def __init__(self) -> None:
        self.insert_count = 0
        self.rowcount = 0
        self.inserted: list[tuple[Any, Any, Any]] = []
        self._existing: set[tuple[Any, Any, Any]] = set()
        self._pending_result: tuple[int] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        statement = sql.strip().upper()
        if statement.startswith("SELECT"):
            # exists_sql params: (tenant, person, source_type, source_id, external_id)
            key = (params[0], params[1], params[4])
            self._pending_result = (1,) if key in self._existing else None
        elif statement.startswith("INSERT"):
            # insert_sql params: (source_type, source_id, tenant, external_id, person, author, reason)
            key = (params[2], params[4], params[3])
            self._existing.add(key)
            self.inserted.append(key)
            self.insert_count += 1
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL in seed_login_ids: {sql}")

    def fetchone(self) -> tuple[int] | None:
        return self._pending_result


class SeedLoginIdsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_auth_mode = os.environ.get("AUTH_MODE")

    def tearDown(self) -> None:
        if self._prev_auth_mode is None:
            os.environ.pop("AUTH_MODE", None)
        else:
            os.environ["AUTH_MODE"] = self._prev_auth_mode

    def test_second_run_does_not_insert_a_duplicate(self) -> None:
        os.environ["AUTH_MODE"] = "fakeidp"
        cur = _FakeCursor()
        roster = _roster()

        first_run_count = identity.seed_login_ids(cur, _TENANT, roster)
        second_run_count = identity.seed_login_ids(cur, _TENANT, roster)

        self.assertEqual(first_run_count, 1, "fakeidp seeds only the dev lead")
        self.assertEqual(second_run_count, 0, "re-run must be a no-op, not a duplicate insert")
        self.assertEqual(cur.insert_count, 1, "only one INSERT should ever have executed")

    def test_keycloak_seeds_the_whole_roster(self) -> None:
        os.environ["AUTH_MODE"] = "keycloak"
        cur = _FakeCursor()
        roster = _roster()

        first_run_count = identity.seed_login_ids(cur, _TENANT, roster)
        second_run_count = identity.seed_login_ids(cur, _TENANT, roster)

        self.assertEqual(
            first_run_count,
            len(roster),
            "keycloak seeds every roster persona (gen-realm.py registers all of them)",
        )
        self.assertEqual(second_run_count, 0, "re-run must be a no-op for every pair")
        self.assertEqual(cur.insert_count, len(roster))

    def test_keycloak_seeds_the_other_tenant_caller_under_its_own_tenant(self) -> None:
        """The second tenant's lone persona gets a login-bootstrap row too.

        They exist ONLY to be refused across a tenant boundary, which they can
        only demonstrate by logging in first: gen-realm.py registers them in
        the realm with their roster uuid as the Keycloak id, so identity must
        hold the matching `value_type='id'` row or the authenticator denies
        them at the callback and the refusal is never reached.
        """
        os.environ["AUTH_MODE"] = "keycloak"
        cur = _FakeCursor()
        other_roster = profiles.build_other_tenant_roster()

        identity.seed_login_ids(cur, _TENANT, _roster())
        inserted = identity.seed_login_ids(cur, profiles.TENANT_OTHER, other_roster)

        self.assertEqual(inserted, len(other_roster))
        self.assertIn(
            (
                identity._bin(profiles.TENANT_OTHER),
                identity._bin(profiles.OTHER_TENANT_PERSON_UUID),
                profiles.OTHER_TENANT_PERSON_UUID,
            ),
            cur.inserted,
            "the row must be keyed to that person's own uuid, under their own tenant",
        )
        self.assertEqual(
            identity.seed_login_ids(cur, profiles.TENANT_OTHER, other_roster),
            0,
            "re-run must be a no-op across tenants too",
        )

    def test_fakeidp_writes_nothing_for_a_roster_it_has_no_identity_for(self) -> None:
        """fakeidp defines no identity for the second tenant's persona.

        The pairs must follow the roster handed in, not a fixed answer: a
        fakeidp branch that ignored its argument would repeat the DEV LEAD's
        `(source_type, external_id)` under the second tenant. The login
        resolve is tenant-agnostic by design, so that duplicate would make the
        dev lead's own login ambiguous — a far worse failure than the second
        tenant's persona being unable to log in on a stand whose IdP has never
        heard of them.
        """
        os.environ["AUTH_MODE"] = "fakeidp"
        cur = _FakeCursor()

        identity.seed_login_ids(cur, _TENANT, _roster())
        inserted = identity.seed_login_ids(
            cur, profiles.TENANT_OTHER, profiles.build_other_tenant_roster()
        )

        self.assertEqual(inserted, 0)
        self.assertEqual(
            [row for row in cur.inserted if row[0] == identity._bin(profiles.TENANT_OTHER)],
            [],
            "no row of any kind belongs to the second tenant under fakeidp",
        )
        self.assertEqual(cur.insert_count, 1, "only the dev lead's own row was written")


if __name__ == "__main__":
    unittest.main()
