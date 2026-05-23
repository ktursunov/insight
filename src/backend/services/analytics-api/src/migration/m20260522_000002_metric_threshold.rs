//! Create `metric_threshold` — per-scope thresholds with v1 lock-bounded escalation.
//!
//! Refs #519. Schema source: `docs/domain/metric-catalog/specs/DESIGN.md` §3.7
//! (`cpt-metric-cat-dbtable-metric-threshold`).
//!
//! Notes that drive non-obvious choices:
//!
//! - `role_slug` / `team_id` are `NOT NULL DEFAULT ''` (empty-string sentinel),
//!   not nullable. SQL treats NULLs as distinct, which would let duplicate
//!   `product-default` rows past the UNIQUE composite — sentinels make the
//!   composite actually unique. See DESIGN §3.7 lines 1011-1012, 1029.
//! - `is_locked_persisted` is a STORED generated mirror of `is_locked`. MariaDB
//!   has no native partial indexes; the lock-enforcer's "find broader-scope
//!   locked row" lookup uses `(tenant_id, metric_key, scope, is_locked_persisted)`
//!   as the supported workaround. See DESIGN §3.7 lines 1021, 1041.
//! - CHECK names below match `REQUIRED_CHECKS` and the startup probe.

use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;

/// Names of every CHECK constraint this migration adds. The startup probe
/// asserts each one is present in `INFORMATION_SCHEMA.CHECK_CONSTRAINTS`.
pub const REQUIRED_CHECKS: &[&str] = &[
    "chk_metric_threshold_lock_reason_when_locked",
    "chk_metric_threshold_lock_scope_v1",
    "chk_metric_threshold_lock_reason_length",
    "chk_metric_threshold_role_slug_shape",
    "chk_metric_threshold_team_id_shape",
];

/// CHECK clause SQL, ordered to match [`REQUIRED_CHECKS`].
const CHECK_CLAUSES: &[(&str, &str)] = &[
    (
        "chk_metric_threshold_lock_reason_when_locked",
        "is_locked = FALSE OR lock_reason IS NOT NULL",
    ),
    (
        "chk_metric_threshold_lock_scope_v1",
        "is_locked = FALSE OR scope IN ('product-default','tenant')",
    ),
    (
        "chk_metric_threshold_lock_reason_length",
        "lock_reason IS NULL OR CHAR_LENGTH(lock_reason) <= 512",
    ),
    (
        "chk_metric_threshold_role_slug_shape",
        "(scope IN ('role','team+role') AND role_slug <> '') \
         OR (scope NOT IN ('role','team+role') AND role_slug = '')",
    ),
    (
        "chk_metric_threshold_team_id_shape",
        "(scope IN ('team','team+role') AND team_id <> '') \
         OR (scope NOT IN ('team','team+role') AND team_id = '')",
    ),
];

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    #[allow(clippy::too_many_lines)] // single-table DDL — splitting hurts readability
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .create_table(
                Table::create()
                    .table(MetricThreshold::Table)
                    .if_not_exists()
                    .col(
                        ColumnDef::new(MetricThreshold::Id)
                            .binary_len(16)
                            .not_null()
                            .primary_key(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::TenantId)
                            .binary_len(16)
                            .null(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::MetricKey)
                            .string_len(128)
                            .not_null(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::Scope)
                            .custom(Alias::new(
                                "ENUM('product-default','tenant','role','team','team+role')",
                            ))
                            .not_null(),
                    )
                    // Empty-string sentinel — NOT NULL DEFAULT '' so the UNIQUE
                    // composite on (tenant_id, metric_key, scope, role_slug, team_id)
                    // doesn't degrade to "NULLs are distinct".
                    .col(
                        ColumnDef::new(MetricThreshold::RoleSlug)
                            .string_len(64)
                            .not_null()
                            .default(""),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::TeamId)
                            .string_len(64)
                            .not_null()
                            .default(""),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::Good)
                            .decimal_len(20, 6)
                            .not_null(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::Warn)
                            .decimal_len(20, 6)
                            .not_null(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::AlertTrigger)
                            .decimal_len(20, 6)
                            .null(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::AlertBad)
                            .decimal_len(20, 6)
                            .null(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::IsLocked)
                            .boolean()
                            .not_null()
                            .default(false),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::LockedBy)
                            .string_len(128)
                            .null(),
                    )
                    .col(ColumnDef::new(MetricThreshold::LockedAt).timestamp().null())
                    .col(
                        ColumnDef::new(MetricThreshold::LockReason)
                            .string_len(512)
                            .null(),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::CreatedAt)
                            .timestamp()
                            .not_null()
                            .default(Expr::current_timestamp()),
                    )
                    .col(
                        ColumnDef::new(MetricThreshold::UpdatedAt)
                            .timestamp()
                            .not_null()
                            .default(Expr::current_timestamp())
                            .extra("ON UPDATE CURRENT_TIMESTAMP"),
                    )
                    .to_owned(),
            )
            .await?;

        let conn = manager.get_connection();

        // Generated column added separately — sea-orm 1.1's column builder doesn't
        // emit MariaDB's `GENERATED ALWAYS AS (...) STORED` cleanly with the
        // NOT-NULL position we need. Raw SQL keeps the produced DDL unambiguous.
        conn.execute_unprepared(
            "ALTER TABLE metric_threshold \
             ADD COLUMN is_locked_persisted BOOLEAN \
             GENERATED ALWAYS AS (is_locked) STORED NOT NULL",
        )
        .await?;

        // UNIQUE composite doubles as the resolver lookup index (§3.7 line 1040).
        manager
            .create_index(
                Index::create()
                    .name("uq_metric_threshold_scope_target")
                    .table(MetricThreshold::Table)
                    .col(MetricThreshold::TenantId)
                    .col(MetricThreshold::MetricKey)
                    .col(MetricThreshold::Scope)
                    .col(MetricThreshold::RoleSlug)
                    .col(MetricThreshold::TeamId)
                    .unique()
                    .to_owned(),
            )
            .await?;

        // Lock-enforcer hot-path index (partial-index emulation via the generated
        // column — §3.7 line 1041). Built via raw SQL so we can reference the
        // generated column without re-declaring it in the SeaORM `Iden` enum.
        conn.execute_unprepared(
            "CREATE INDEX idx_metric_threshold_lock_enforcer \
             ON metric_threshold (tenant_id, metric_key, scope, is_locked_persisted)",
        )
        .await?;

        for (name, predicate) in CHECK_CLAUSES {
            conn.execute_unprepared(&format!(
                "ALTER TABLE metric_threshold ADD CONSTRAINT {name} CHECK ({predicate})"
            ))
            .await?;
        }

        Ok(())
    }

    async fn down(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        // DROP TABLE cascades to CHECK constraints, indexes, and the generated
        // `is_locked_persisted` column — all of which we added via raw SQL after
        // `create_table`. Keep this in sync if a future migration adds FKs
        // *into* metric_threshold (e.g., a role_catalog FK on `role_slug`):
        // the drop will start failing and you'll need an explicit ALTER ...
        // DROP FOREIGN KEY first.
        manager
            .drop_table(Table::drop().table(MetricThreshold::Table).to_owned())
            .await?;
        Ok(())
    }
}

#[derive(DeriveIden)]
enum MetricThreshold {
    Table,
    Id,
    TenantId,
    MetricKey,
    Scope,
    RoleSlug,
    TeamId,
    Good,
    Warn,
    AlertTrigger,
    AlertBad,
    IsLocked,
    LockedBy,
    LockedAt,
    LockReason,
    // Generated mirror of `IsLocked`; added via raw SQL after table creation
    // (see comment near the ALTER TABLE call). Listed here so it shows up in
    // grep / IDE refactors even though no in-crate code currently references
    // it through SeaORM.
    #[allow(dead_code)]
    IsLockedPersisted,
    CreatedAt,
    UpdatedAt,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn predicate_for(name: &str) -> &'static str {
        let Some((_, p)) = CHECK_CLAUSES.iter().find(|(n, _)| *n == name) else {
            panic!("CHECK {name} not registered in CHECK_CLAUSES");
        };
        p
    }

    #[test]
    fn check_clauses_match_required_list() {
        let clause_names: Vec<&str> = CHECK_CLAUSES.iter().map(|(n, _)| *n).collect();
        assert_eq!(
            clause_names.as_slice(),
            REQUIRED_CHECKS,
            "CHECK_CLAUSES names must equal REQUIRED_CHECKS in the same order — \
             the startup probe relies on this list to detect drops"
        );
    }

    #[test]
    fn lock_reason_when_locked_predicate_is_correct() {
        let p = predicate_for("chk_metric_threshold_lock_reason_when_locked");
        assert!(p.contains("is_locked = FALSE"));
        assert!(p.contains("lock_reason IS NOT NULL"));
    }

    #[test]
    fn v1_lock_scope_restricted_to_product_default_and_tenant() {
        let p = predicate_for("chk_metric_threshold_lock_scope_v1");
        assert!(p.contains("'product-default'"));
        assert!(p.contains("'tenant'"));
        // v1: role / team / team+role MUST NOT appear here. Adding them would
        // permit the lock-escalation path admin-crud is meant to block.
        assert!(!p.contains("'role'"));
        assert!(!p.contains("'team'"));
        assert!(!p.contains("'team+role'"));
    }

    #[test]
    fn role_slug_shape_predicate_uses_sentinel_logic() {
        let p = predicate_for("chk_metric_threshold_role_slug_shape");
        assert!(p.contains("role_slug <> ''"));
        assert!(p.contains("role_slug = ''"));
        // Must enumerate role-bearing scopes.
        assert!(p.contains("'role'"));
        assert!(p.contains("'team+role'"));
    }

    #[test]
    fn team_id_shape_predicate_uses_sentinel_logic() {
        let p = predicate_for("chk_metric_threshold_team_id_shape");
        assert!(p.contains("team_id <> ''"));
        assert!(p.contains("team_id = ''"));
        assert!(p.contains("'team'"));
        assert!(p.contains("'team+role'"));
    }
}
