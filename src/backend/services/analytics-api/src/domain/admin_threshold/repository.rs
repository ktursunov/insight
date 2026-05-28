//! Repository — SeaORM CRUD against `metric_threshold` + the joined
//! `metric_catalog.schema_*` read.
//!
//! Owns the SQL the gauntlet needs:
//!
//! - `find_metric_catalog(metric_id)` — referential-integrity check at the
//!   start of every write. Returns `(metric_key, is_enabled)` so the
//!   gauntlet can reject unknown / disabled metrics with a structured
//!   `invalid_argument` before touching `metric_threshold`.
//! - `find_threshold(id)` — GET-by-id + the "row's `tenant_id` vs caller's
//!   tenant" check.
//! - `list_thresholds(tenant_id, filters)` — list with the in-spec filter
//!   set (`metric_id`, `scope`, `role_slug`, `team_id`); `tenant_id` is
//!   ALWAYS derived from `SecurityContext`.
//! - `insert_threshold` / `update_threshold` / `delete_threshold` — write
//!   paths. The lock-transition writes are run inside the caller's TX so
//!   `audit_emitter::emit_lock_transition_in_tx` can atomically attach
//!   the audit row.
//!
//! Every UUID bind uses `Value::Bytes(uuid.as_bytes().to_vec())` so the
//! BINARY(16) tenant / id columns hit the existing index plans (same
//! convention as `domain/catalog/resolver.rs::bulk_fetch`).

use chrono::{DateTime, Utc};
use sea_orm::{
    ActiveModelTrait, ActiveValue, ColumnTrait, ConnectionTrait, DatabaseConnection,
    DatabaseTransaction, EntityTrait, FromQueryResult, QueryFilter, Statement, TransactionTrait,
    Value,
};
use uuid::Uuid;

use crate::domain::admin_threshold::dto::{ListFilters, Scope, ThresholdView};
use crate::infra::db::entities::metric_threshold;

/// A minimal projection of `metric_catalog` for the gauntlet's pre-write
/// referential-integrity check.
#[derive(Debug, Clone)]
pub struct CatalogLookup {
    pub metric_key: String,
    pub is_enabled: bool,
    pub schema_status: String,
    pub schema_error_code: Option<String>,
}

/// `SELECT metric_key, is_enabled, schema_status, schema_error_code
///  FROM metric_catalog WHERE id = ?`. Returns `Ok(None)` for unknown
/// `metric_id` so the gauntlet can emit `invalid_argument` with
/// `reason = UNKNOWN_OR_DISABLED`.
///
/// Raw SQL (not the SeaORM entity) because the typed `metric_catalog::Model`
/// declared in #519 only exposes the columns the schema-validator
/// touches — adding `is_enabled` would reshape an entity owned by
/// another component for a single boolean.
///
/// # Errors
///
/// Surfaces SeaORM connection / decode errors. Caller maps to a 5xx.
pub async fn find_metric_catalog(
    db: &DatabaseConnection,
    metric_id: Uuid,
) -> Result<Option<CatalogLookup>, sea_orm::DbErr> {
    let backend = db.get_database_backend();
    let stmt = Statement::from_sql_and_values(
        backend,
        "SELECT metric_key, is_enabled, schema_status, schema_error_code \
         FROM metric_catalog WHERE id = ?",
        [Value::Bytes(Some(Box::new(metric_id.as_bytes().to_vec())))],
    );
    let row = CatalogFullRow::find_by_statement(stmt).one(db).await?;
    Ok(row.map(|r| CatalogLookup {
        metric_key: r.metric_key,
        is_enabled: r.is_enabled,
        schema_status: r.schema_status,
        schema_error_code: r.schema_error_code,
    }))
}

#[derive(Debug, FromQueryResult)]
struct CatalogFullRow {
    metric_key: String,
    is_enabled: bool,
    schema_status: String,
    schema_error_code: Option<String>,
}

/// Reverse lookup: find the `metric_catalog` row whose `metric_key`
/// matches `metric_key` — used by the GET-by-id + list paths to attach
/// the `schema_status` join columns and the `metric_id` UUID to each
/// `ThresholdView`.
///
/// # Errors
///
/// Surfaces SeaORM connection / decode errors.
pub async fn find_catalog_id_for_metric_key(
    db: &DatabaseConnection,
    metric_key: &str,
) -> Result<Option<CatalogJoinRow>, sea_orm::DbErr> {
    let backend = db.get_database_backend();
    let stmt = Statement::from_sql_and_values(
        backend,
        "SELECT id AS id, schema_status AS schema_status, schema_error_code AS schema_error_code \
         FROM metric_catalog WHERE metric_key = ?",
        [Value::from(metric_key)],
    );
    CatalogJoinRow::find_by_statement(stmt).one(db).await
}

#[derive(Debug, Clone, FromQueryResult)]
pub struct CatalogJoinRow {
    pub id: Uuid,
    pub schema_status: String,
    pub schema_error_code: Option<String>,
}

/// Fetch a threshold row by id. Returns `Ok(None)` when missing —
/// caller emits `not_found`.
///
/// # Errors
///
/// Surfaces SeaORM connection / decode errors.
pub async fn find_threshold(
    db: &DatabaseConnection,
    id: Uuid,
) -> Result<Option<metric_threshold::Model>, sea_orm::DbErr> {
    metric_threshold::Entity::find_by_id(id).one(db).await
}

/// List threshold rows for `tenant_id`, applying the in-spec filter set.
///
/// The shape of the query is parameterized — we build the WHERE
/// dynamically from the optional filters but ALWAYS bind `tenant_id`
/// against the `tenant_id_sentinel` generated column (so
/// `product-default` rows whose `tenant_id IS NULL` also match the
/// sentinel-zero bind when the caller wants to see them).
///
/// **`product-default` rows are NOT listed for tenant callers.** DESIGN
/// §3.3 lists are scoped to "the caller's tenant"; product-default rows
/// are global. The list intentionally excludes them — operators query
/// `product-default` via the seed-migration source of truth, not the
/// admin surface.
///
/// # Errors
///
/// Surfaces SeaORM connection / decode errors.
pub async fn list_thresholds(
    db: &DatabaseConnection,
    tenant_id: Uuid,
    filters: &ListFilters,
) -> Result<Vec<metric_threshold::Model>, sea_orm::DbErr> {
    // If `metric_id` is set, resolve it to a `metric_key` first; the
    // threshold rows live by string `metric_key`, no FK. An unknown
    // `metric_id` yields zero rows rather than an error — same
    // behavior as filtering by a `metric_key` that doesn't exist.
    let metric_key_filter = match filters.metric_id {
        Some(mid) => match find_metric_catalog(db, mid).await? {
            Some(cat) => Some(cat.metric_key),
            None => return Ok(Vec::new()),
        },
        None => None,
    };

    let mut q =
        metric_threshold::Entity::find().filter(metric_threshold::Column::TenantId.eq(tenant_id));
    if let Some(mk) = metric_key_filter {
        q = q.filter(metric_threshold::Column::MetricKey.eq(mk));
    }
    if let Some(scope) = filters.scope {
        q = q.filter(metric_threshold::Column::Scope.eq(scope.as_db_str()));
    }
    if let Some(role_slug) = filters.role_slug.as_deref() {
        q = q.filter(metric_threshold::Column::RoleSlug.eq(role_slug));
    }
    if let Some(team_id) = filters.team_id.as_deref() {
        q = q.filter(metric_threshold::Column::TeamId.eq(team_id));
    }
    q.all(db).await
}

/// Insert a new row inside `tx`. The caller is in the same transaction
/// the audit-emitter writes to on `lock_set`, so a TX rollback handles
/// the atomic-with-write contract.
///
/// # Errors
///
/// Surfaces SeaORM insert / CHECK-violation errors — caller maps to a
/// canonical 4xx via `error_map`.
#[allow(clippy::too_many_arguments)] // single-row INSERT, every column is meaningful
pub async fn insert_threshold(
    tx: &DatabaseTransaction,
    id: Uuid,
    tenant_id: Uuid,
    metric_key: &str,
    scope: Scope,
    role_slug: &str,
    team_id: &str,
    good: f64,
    warn: f64,
    alert_trigger: Option<f64>,
    alert_bad: Option<f64>,
    is_locked: bool,
    locked_by: Option<String>,
    locked_at: Option<DateTime<Utc>>,
    lock_reason: Option<String>,
) -> Result<metric_threshold::Model, sea_orm::DbErr> {
    let now = Utc::now();
    let model = metric_threshold::ActiveModel {
        id: ActiveValue::Set(id),
        tenant_id: ActiveValue::Set(Some(tenant_id)),
        metric_key: ActiveValue::Set(metric_key.to_owned()),
        scope: ActiveValue::Set(scope.as_db_str().to_owned()),
        role_slug: ActiveValue::Set(role_slug.to_owned()),
        team_id: ActiveValue::Set(team_id.to_owned()),
        good: ActiveValue::Set(good),
        warn: ActiveValue::Set(warn),
        alert_trigger: ActiveValue::Set(alert_trigger),
        alert_bad: ActiveValue::Set(alert_bad),
        is_locked: ActiveValue::Set(is_locked),
        locked_by: ActiveValue::Set(locked_by),
        locked_at: ActiveValue::Set(locked_at),
        lock_reason: ActiveValue::Set(lock_reason),
        created_at: ActiveValue::Set(now),
        updated_at: ActiveValue::Set(now),
    };
    let res = metric_threshold::Entity::insert(model)
        .exec_with_returning(tx)
        .await?;
    Ok(res)
}

/// Update the mutable fields of an existing row. `scope` / `role_slug`
/// / `team_id` are immutable post-create — the gauntlet rejects PUTs
/// that change them BEFORE this is called, so the values from the
/// existing row are passed back verbatim for the row's update.
///
/// # Errors
///
/// Surfaces SeaORM update / CHECK-violation errors.
#[allow(clippy::too_many_arguments)]
pub async fn update_threshold(
    tx: &DatabaseTransaction,
    id: Uuid,
    good: f64,
    warn: f64,
    alert_trigger: Option<f64>,
    alert_bad: Option<f64>,
    is_locked: bool,
    locked_by: Option<String>,
    locked_at: Option<DateTime<Utc>>,
    lock_reason: Option<String>,
) -> Result<metric_threshold::Model, sea_orm::DbErr> {
    let existing = metric_threshold::Entity::find_by_id(id)
        .one(tx)
        .await?
        .ok_or_else(|| sea_orm::DbErr::RecordNotFound(format!("metric_threshold {id}")))?;
    let mut am: metric_threshold::ActiveModel = existing.into();
    am.good = ActiveValue::Set(good);
    am.warn = ActiveValue::Set(warn);
    am.alert_trigger = ActiveValue::Set(alert_trigger);
    am.alert_bad = ActiveValue::Set(alert_bad);
    am.is_locked = ActiveValue::Set(is_locked);
    am.locked_by = ActiveValue::Set(locked_by);
    am.locked_at = ActiveValue::Set(locked_at);
    am.lock_reason = ActiveValue::Set(lock_reason);
    // `updated_at` has `ON UPDATE CURRENT_TIMESTAMP` in the schema so we
    // don't set it here — the DB stamps it on its own.
    let updated = am.update(tx).await?;
    Ok(updated)
}

/// Delete by id. Returns the number of rows deleted (0 ⇒ not found).
///
/// # Errors
///
/// Surfaces SeaORM transport / query errors.
pub async fn delete_threshold(tx: &DatabaseTransaction, id: Uuid) -> Result<u64, sea_orm::DbErr> {
    let res = metric_threshold::Entity::delete_by_id(id).exec(tx).await?;
    Ok(res.rows_affected)
}

/// Begin a transaction. Thin wrapper so the service code doesn't import
/// `TransactionTrait` directly.
///
/// # Errors
///
/// Surfaces SeaORM transaction begin failures.
pub async fn begin_tx(db: &DatabaseConnection) -> Result<DatabaseTransaction, sea_orm::DbErr> {
    db.begin().await
}

/// Project a `metric_threshold::Model` + the joined `metric_catalog` row
/// into the wire shape. `role_slug` / `team_id` empty-string sentinels
/// collapse to `None` so the JSON carries `null` instead of `""` (FE
/// "is this set?" predicates work cleanly).
///
/// # Errors
///
/// Returns `DbErr` when the row's `scope` column is not a known enum
/// value — schema drift the caller surfaces as a 5xx (`get_one`) or
/// skip+log (`list`). Silently coercing to `ProductDefault` would put a
/// misleading scope on the wire.
pub fn view_from_model(
    row: &metric_threshold::Model,
    catalog: &CatalogJoinRow,
) -> Result<ThresholdView, sea_orm::DbErr> {
    let scope = Scope::from_db_str(&row.scope).ok_or_else(|| {
        sea_orm::DbErr::Custom(format!(
            "metric_threshold row {} has unknown scope {:?} (DB drift)",
            row.id, row.scope
        ))
    })?;
    Ok(ThresholdView {
        id: row.id,
        tenant_id: row.tenant_id,
        metric_id: catalog.id,
        scope,
        role_slug: (!row.role_slug.is_empty()).then(|| row.role_slug.clone()),
        team_id: (!row.team_id.is_empty()).then(|| row.team_id.clone()),
        good: row.good,
        warn: row.warn,
        alert_trigger: row.alert_trigger,
        alert_bad: row.alert_bad,
        is_locked: row.is_locked,
        locked_by: row.locked_by.clone(),
        locked_at: row.locked_at,
        lock_reason: row.lock_reason.clone(),
        schema_status: catalog.schema_status.clone(),
        schema_error_code: catalog.schema_error_code.clone(),
    })
}
