{# -------------------------------------------------------------------------
   Bootstrap model for GitHub Copilot bronze → RMT promotion.

   Counterpart of `jira__bronze_promoted` / `confluence__bronze_promoted` for
   GitHub Copilot. See ADR-0002. The `promote_bronze_to_rmt` macro is
   idempotent — already-RMT tables are detected and skipped on subsequent
   runs.
   ------------------------------------------------------------------------- #}

-- @cpt-principle:cpt-dataflow-principle-promote-bronze:p1
{{ config(
    materialized='view',
    schema='staging',
    tags=['github-copilot']
) }}

{% do promote_bronze_to_rmt(table='bronze_github_copilot.copilot_seats',         order_by='unique_key') %}
{% do promote_bronze_to_rmt(table='bronze_github_copilot.copilot_user_metrics',  order_by='unique_key') %}
{% do promote_bronze_to_rmt(table='bronze_github_copilot.copilot_org_metrics',   order_by='unique_key') %}

SELECT 1 AS promoted
