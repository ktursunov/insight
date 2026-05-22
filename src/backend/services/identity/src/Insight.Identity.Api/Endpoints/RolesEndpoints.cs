using FluentValidation;
using Insight.Identity.Api.Auth;
using Insight.Identity.Api.Contracts;
using Insight.Identity.Domain.Services;
using Insight.Identity.Infrastructure.MariaDb;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.Logging;

namespace Insight.Identity.Api.Endpoints;

/// <summary>
/// CRUD over the <c>roles</c> catalogue. Admin-only; hard-DELETE with
/// a 422 <c>urn:insight:error:role_in_use</c> guard against orphaning
/// active <c>person_roles</c> assignments. See ADR-0013.
/// </summary>
public static class RolesEndpoints
{
    private const string RoleNameExistsUrn = "urn:insight:error:role_name_exists";
    private const string RoleInUseUrn      = "urn:insight:error:role_in_use";

    public static IEndpointRouteBuilder MapRoleEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        app.MapPost("/v1/roles", async (
            CreateRoleCommandModel body,
            HttpContext http,
            CallerAdminCheck admin,
            IValidator<CreateRoleCommandModel> validator,
            RolesRepository repo,
            ILoggerFactory loggerFactory,
            CancellationToken ct) =>
        {
            var gate = await admin.CheckAsync(http, ct).ConfigureAwait(false);
            if (gate is not AdminCheckResult.IsAdmin) return EndpointHelpers.GateResult(gate);

            var validation = await validator.ValidateAsync(body, ct).ConfigureAwait(false);
            if (!validation.IsValid) return EndpointHelpers.ValidationFailure(validation);

            // Pre-check for duplicate name so the response is 409 with
            // a friendly URN; the UNIQUE(name) index would also reject
            // but surface as an opaque 500 MySqlException.
            var existing = await repo.GetByNameAsync(body.Name, ct).ConfigureAwait(false);
            if (existing is not null)
            {
                return Results.Json(new ProblemResponse(
                    Type: RoleNameExistsUrn,
                    Title: "Conflict",
                    Status: StatusCodes.Status409Conflict,
                    Detail: $"role name '{body.Name}' already exists"),
                    statusCode: StatusCodes.Status409Conflict);
            }

            var id = await repo.InsertRoleAsync(body.Name, ct).ConfigureAwait(false);
            EndpointHelpers.Audit(loggerFactory, "roles.create",
                ("role_id", id),
                ("name", body.Name),
                ("author_person_id", EndpointHelpers.ResolveCaller(http)!.Value));
            return Results.Created($"/v1/roles/{id:D}", new RoleResponse(id, body.Name));
        });

        app.MapGet("/v1/roles", async (
            HttpContext http,
            CallerAdminCheck admin,
            RolesRepository repo,
            CancellationToken ct) =>
        {
            var gate = await admin.CheckAsync(http, ct).ConfigureAwait(false);
            if (gate is not AdminCheckResult.IsAdmin) return EndpointHelpers.GateResult(gate);

            var roles = await repo.ListAllAsync(ct).ConfigureAwait(false);
            var items = roles.Select(RoleResponse.From).ToList();
            return Results.Ok(new ListResponse<RoleResponse>(items, NextCursor: null));
        });

        app.MapDelete("/v1/roles/{id}", async (
            Guid id,
            HttpContext http,
            CallerAdminCheck admin,
            RolesRepository repo,
            ILoggerFactory loggerFactory,
            CancellationToken ct) =>
        {
            var gate = await admin.CheckAsync(http, ct).ConfigureAwait(false);
            if (gate is not AdminCheckResult.IsAdmin) return EndpointHelpers.GateResult(gate);

            // Pre-fetch for audit name + initial 404; not part of the
            // guard. The atomic DELETE below owns the in-use check —
            // `roles` is strict-minimum (no valid_to slot, see ADR-0013)
            // so DELETE is hard and orphaning person_roles would be
            // silent without an atomic refusal.
            var existing = await repo.GetRoleByIdAsync(id, ct).ConfigureAwait(false);
            if (existing is null) return EndpointHelpers.NotFound("role", id);

            var rowsAffected = await repo.TryDeleteRoleIfUnusedAsync(id, ct).ConfigureAwait(false);
            if (rowsAffected == 1)
            {
                EndpointHelpers.Audit(loggerFactory, "roles.delete",
                    ("role_id", id),
                    ("name", existing.Name),
                    ("author_person_id", EndpointHelpers.ResolveCaller(http)!.Value));
                return Results.NoContent();
            }

            // rowsAffected == 0: either the role vanished between
            // pre-fetch and DELETE (treat as 404) or the in-use guard
            // fired (422). A second read + count tells us which and
            // supplies the assignment count for the 422 message.
            var refetched = await repo.GetRoleByIdAsync(id, ct).ConfigureAwait(false);
            if (refetched is null) return EndpointHelpers.NotFound("role", id);
            var live = await repo.CountActiveAssignmentsByRoleAnyTenantAsync(id, ct).ConfigureAwait(false);
            return Results.Json(new ProblemResponse(
                Type: RoleInUseUrn,
                Title: "Unprocessable Entity",
                Status: StatusCodes.Status422UnprocessableEntity,
                Detail: $"role has {live} active assignment(s); revoke them before deletion"),
                statusCode: StatusCodes.Status422UnprocessableEntity);
        });

        return app;
    }
}
