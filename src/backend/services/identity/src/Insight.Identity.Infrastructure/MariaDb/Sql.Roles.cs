namespace Insight.Identity.Infrastructure.MariaDb;

/// <summary>
/// SQL for the `roles` and `person_roles` tables (#346 step 1).
/// `roles` is global (no tenant column); `person_roles` is per-tenant.
/// </summary>
internal static class SqlRoles
{
    public const string RoleByName = """
        SELECT role_id, name
        FROM roles
        WHERE name = @name
        LIMIT 1
        """;

    public const string ListAllRoles = """
        SELECT role_id, name
        FROM roles
        ORDER BY name
        """;

    public const string HasActivePersonRole = """
        SELECT EXISTS (
            SELECT 1
            FROM person_roles
            WHERE insight_tenant_id = @tenant_id
              AND person_id         = @person_id
              AND role_id           = @role_id
              AND valid_to IS NULL
        )
        """;

    public const string ActivePersonRolesByPerson = """
        SELECT person_role_id, insight_tenant_id, person_id, role_id,
               valid_from, valid_to, author_person_id, reason, created_at
        FROM person_roles
        WHERE insight_tenant_id = @tenant_id
          AND person_id         = @person_id
          AND valid_to IS NULL
        """;

    public const string RoleById = """
        SELECT role_id, name
        FROM roles
        WHERE role_id = @role_id
        LIMIT 1
        """;

    public const string InsertRole = """
        INSERT INTO roles (role_id, name)
        VALUES (@role_id, @name)
        """;

    // Atomic delete-if-unused: refuse if any active `person_roles` row
    // references the role (any tenant). One round-trip — no separate
    // COUNT call, so no TOCTOU race between guard and write. Disambiguate
    // rows_affected==0 in the caller via a second read.
    public const string TryDeleteRoleIfUnused = """
        DELETE FROM roles
        WHERE role_id = @role_id
          AND NOT EXISTS (
              SELECT 1 FROM person_roles
              WHERE role_id = @role_id AND valid_to IS NULL
          )
        """;

    public const string CountActivePersonRolesByRole = """
        SELECT COUNT(*)
        FROM person_roles
        WHERE insight_tenant_id = @tenant_id
          AND role_id           = @role_id
          AND valid_to IS NULL
        """;

    public const string CountActivePersonRolesByRoleAnyTenant = """
        SELECT COUNT(*)
        FROM person_roles
        WHERE role_id    = @role_id
          AND valid_to IS NULL
        """;

    private const string PersonRoleColumnList =
        "person_role_id, insight_tenant_id, person_id, role_id, " +
        "valid_from, valid_to, author_person_id, reason, created_at";

    public const string PersonRoleById = $"""
        SELECT {PersonRoleColumnList}
        FROM person_roles
        WHERE person_role_id = @person_role_id
        LIMIT 1
        """;

    public const string PersonRoleListBase = $"""
        SELECT {PersonRoleColumnList}
        FROM person_roles
        WHERE insight_tenant_id = @tenant_id
        """;

    public const string InsertPersonRole = """
        INSERT INTO person_roles
            (person_role_id, insight_tenant_id, person_id, role_id,
             valid_from, valid_to, author_person_id, reason)
        VALUES
            (@person_role_id, @tenant_id, @person_id, @role_id,
             IFNULL(@valid_from, UTC_TIMESTAMP(6)), NULL, @author_person_id, @reason)
        """;

    // Atomic soft-delete with last-admin protection. One round-trip:
    // the UPDATE refuses to fire when (a) the row is the only active
    // admin in its tenant, OR (b) the row is already revoked / missing.
    // Strategy: a single derived table (`row_with_count`) computes both
    // the row-to-revoke metadata AND the active-admin count for the
    // row's tenant in one shot — the correlated COUNT lives inside the
    // derived table's SELECT list, where MariaDB resolves it before the
    // UPDATE acts. The outer UPDATE then sees `row_with_count` as a
    // plain JOINed source, which sidesteps the "cannot SELECT from
    // the table being updated" restriction. Disambiguate
    // rows_affected==0 in the caller (404 vs 422 last_admin_protected)
    // via a second read.
    public const string TrySoftDeletePersonRoleProtectingLastAdmin = """
        UPDATE person_roles AS target
        JOIN (
            SELECT
                pr.person_role_id,
                pr.role_id,
                (
                    SELECT COUNT(*)
                    FROM person_roles AS adm
                    WHERE adm.insight_tenant_id = pr.insight_tenant_id
                      AND adm.role_id           = @admin_role_id
                      AND adm.valid_to IS NULL
                ) AS active_admin_cnt
            FROM person_roles AS pr
            WHERE pr.person_role_id = @person_role_id
              AND pr.valid_to IS NULL
        ) AS row_with_count
          ON row_with_count.person_role_id = target.person_role_id
        SET target.valid_to = UTC_TIMESTAMP(6),
            target.reason   = COALESCE(@reason, target.reason)
        WHERE target.valid_to IS NULL
          AND (
              row_with_count.role_id <> @admin_role_id
              OR row_with_count.active_admin_cnt > 1
          )
        """;
}
