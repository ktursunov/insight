-- =====================================================================
-- metrics-gold-views — backing views for IC and team aggregates
-- =====================================================================
-- Adds:
--   - supervisor_email column to insight.people (sourced from BambooHR)
--   - insight.ic_histogram        (UUID 0…30)
--   - insight.peer_cohort_stats   (UUID 0…34) — aggregate-only
--   - insight.ic_section_trend    (UUID 0…36)
--   - insight.ic_kpi_peer_median  (UUID 0…37) — long-format KPI rows
--
-- All views key on (person_id, org_unit_id, metric_date, …) so the
-- existing OData $filter on `metric_date BETWEEN ?` continues to work.
-- The peer cohort view never selects peer person_id — authorization
-- lives in the view shape.
-- =====================================================================


-- ---------------------------------------------------------------------
-- insight.people — extend with supervisor_email
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS insight.people;
CREATE VIEW insight.people
(
    `person_id` Nullable(String),
    `display_name` Nullable(String),
    `org_unit_id` Nullable(String),
    `org_unit_name` Nullable(String),
    `seniority` String,
    `job_title` Nullable(String),
    `status` Nullable(String),
    `supervisor_email` Nullable(String)
)
AS SELECT
    person_id,
    argMax(displayName, _airbyte_extracted_at) AS display_name,
    argMax(department, _airbyte_extracted_at) AS org_unit_id,
    argMax(department, _airbyte_extracted_at) AS org_unit_name,
    argMax(multiIf((jobTitle ILIKE '%senior%') OR (jobTitle ILIKE '%lead%') OR (jobTitle ILIKE '%principal%') OR (jobTitle ILIKE '%architect%') OR (jobTitle ILIKE '%director%') OR (jobTitle ILIKE '%head%'), 'Senior', (jobTitle ILIKE '%junior%') OR (jobTitle ILIKE '%intern%') OR (jobTitle ILIKE '%trainee%'), 'Junior', 'Mid'), _airbyte_extracted_at) AS seniority,
    argMax(jobTitle, _airbyte_extracted_at) AS job_title,
    argMax(status, _airbyte_extracted_at) AS status,
    lower(argMax(supervisorEmail, _airbyte_extracted_at)) AS supervisor_email
FROM bronze_bamboohr.employees
WHERE (workEmail IS NOT NULL) AND (workEmail != '')
GROUP BY lower(workEmail) AS person_id
;


-- ---------------------------------------------------------------------
-- insight.ic_histogram — bins per distribution metric per person/day
-- ---------------------------------------------------------------------
-- Allowlisted metric_keys with continuous / time / ratio shapes. Step
-- size picked per metric to match the unit's natural granularity.
DROP VIEW IF EXISTS insight.ic_histogram;
CREATE VIEW insight.ic_histogram
AS
WITH all_rows AS (
    SELECT toString(person_id) AS person_id,
           toString(coalesce(org_unit_id, '')) AS org_unit_id,
           toDateOrNull(toString(metric_date)) AS metric_date,
           metric_key,
           toFloat64OrNull(toString(metric_value)) AS metric_value
    FROM insight.task_delivery_bullet_rows
    UNION ALL SELECT toString(person_id), toString(coalesce(org_unit_id, '')),
                     toDateOrNull(toString(metric_date)), metric_key,
                     toFloat64OrNull(toString(metric_value))
        FROM insight.code_quality_bullet_rows
    UNION ALL SELECT toString(person_id), toString(coalesce(org_unit_id, '')),
                     toDateOrNull(toString(metric_date)), metric_key,
                     toFloat64OrNull(toString(metric_value))
        FROM insight.ai_bullet_rows
    UNION ALL SELECT toString(person_id), toString(coalesce(org_unit_id, '')),
                     toDateOrNull(toString(metric_date)), metric_key,
                     toFloat64OrNull(toString(metric_value))
        FROM insight.collab_bullet_rows
    UNION ALL SELECT toString(person_id), toString(coalesce(org_unit_id, '')),
                     toDateOrNull(toString(metric_date)), metric_key,
                     toFloat64OrNull(toString(metric_value))
        FROM insight.git_bullet_rows
),
classified AS (
    SELECT
        person_id, org_unit_id, metric_date, metric_key, metric_value,
        multiIf(
            metric_key IN ('task_dev_time','meeting_hours',
                           'teams_meeting_hours','zoom_meeting_hours'), 4,           -- hours
            metric_key IN ('mean_time_to_resolution','pickup_time'), 1,              -- days
            metric_key IN ('estimation_accuracy','task_reopen_rate',
                           'due_date_compliance','flow_efficiency',
                           'merge_rate','build_success',
                           'cursor_acceptance','cc_tool_accept',
                           'ai_loc_share2','slack_dm_ratio'), 10,                    -- percent
            0
        ) AS step
    FROM all_rows
    WHERE metric_value IS NOT NULL
)
SELECT
    person_id,
    org_unit_id,
    metric_date,
    metric_key,
    toInt64(floor(metric_value / step) * step)              AS bin,
    toInt64(floor(metric_value / step) * step + step)       AS bin_end,
    count()                                                 AS count
FROM classified
WHERE step > 0
GROUP BY person_id, org_unit_id, metric_date, metric_key, bin, bin_end, step
;


-- ---------------------------------------------------------------------
-- insight.peer_cohort_stats — aggregate-only peer percentiles
-- ---------------------------------------------------------------------
-- Two cohort flavours:
--   kind='ic'    cohort_seed = supervisor_email  (people sharing a manager)
--   kind='team'  cohort_seed = org_unit_id       (whole department)
-- View shape never includes peer person_id — authorization stays in the
-- view definition, not the OData handler.
DROP VIEW IF EXISTS insight.peer_cohort_stats;
CREATE VIEW insight.peer_cohort_stats
AS
WITH all_rows AS (
    SELECT br.person_id AS person_id,
           br.metric_date AS metric_date,
           br.metric_key AS metric_key,
           br.metric_value AS metric_value,
           p.org_unit_id AS org_unit_id,
           p.supervisor_email AS supervisor_email
    FROM (
        SELECT toString(person_id) AS person_id,
               toDateOrNull(toString(metric_date)) AS metric_date,
               metric_key,
               toFloat64OrNull(toString(metric_value)) AS metric_value
        FROM insight.task_delivery_bullet_rows
        UNION ALL SELECT toString(person_id),
                         toDateOrNull(toString(metric_date)),
                         metric_key,
                         toFloat64OrNull(toString(metric_value))
            FROM insight.code_quality_bullet_rows
        UNION ALL SELECT toString(person_id),
                         toDateOrNull(toString(metric_date)),
                         metric_key,
                         toFloat64OrNull(toString(metric_value))
            FROM insight.ai_bullet_rows
        UNION ALL SELECT toString(person_id),
                         toDateOrNull(toString(metric_date)),
                         metric_key,
                         toFloat64OrNull(toString(metric_value))
            FROM insight.collab_bullet_rows
        UNION ALL SELECT toString(person_id),
                         toDateOrNull(toString(metric_date)),
                         metric_key,
                         toFloat64OrNull(toString(metric_value))
            FROM insight.git_bullet_rows
    ) br
    LEFT JOIN insight.people AS p ON br.person_id = p.person_id
    WHERE br.metric_value IS NOT NULL
),
ic_cohort AS (
    SELECT
        supervisor_email                                AS cohort_seed,
        'ic'                                            AS kind,
        metric_key,
        metric_date,
        quantileExact(0.25)(metric_value)               AS p25,
        quantileExact(0.50)(metric_value)               AS p50,
        quantileExact(0.75)(metric_value)               AS p75,
        min(metric_value)                               AS min,
        max(metric_value)                               AS max,
        uniqExact(person_id)                            AS n
    FROM all_rows
    WHERE supervisor_email IS NOT NULL AND supervisor_email != ''
    GROUP BY supervisor_email, metric_key, metric_date
),
team_cohort AS (
    SELECT
        org_unit_id                                     AS cohort_seed,
        'team'                                          AS kind,
        metric_key,
        metric_date,
        quantileExact(0.25)(metric_value)               AS p25,
        quantileExact(0.50)(metric_value)               AS p50,
        quantileExact(0.75)(metric_value)               AS p75,
        min(metric_value)                               AS min,
        max(metric_value)                               AS max,
        uniqExact(person_id)                            AS n
    FROM all_rows
    WHERE org_unit_id IS NOT NULL AND org_unit_id != ''
    GROUP BY org_unit_id, metric_key, metric_date
)
SELECT * FROM ic_cohort
UNION ALL
SELECT * FROM team_cohort
;



-- ---------------------------------------------------------------------
-- insight.ic_section_trend — daily time series per (person, section)
-- ---------------------------------------------------------------------
-- Long format. Frontend pivots series_key into wide columns per chart.
-- task_delivery + git_output reuse the existing daily bullet_rows
-- (granularity = day). code_quality / ai_adoption / collaboration pull
-- their representative series from the same bullet_rows.
DROP VIEW IF EXISTS insight.ic_section_trend;
CREATE VIEW insight.ic_section_trend
AS
-- task_delivery: tasks closed daily
SELECT toString(person_id)                       AS person_id,
       toString(coalesce(org_unit_id, ''))       AS org_unit_id,
       toDateOrNull(toString(metric_date))       AS metric_date,
       'task_delivery'                           AS section_id,
       'tasks_completed'                         AS series_key,
       sum(toFloat64OrNull(toString(metric_value))) AS value
FROM insight.task_delivery_bullet_rows
WHERE metric_key = 'tasks_completed'
GROUP BY person_id, org_unit_id, metric_date

UNION ALL
-- git_output: commits + prs_merged daily (from git_bullet_rows)
SELECT toString(person_id),
       toString(coalesce(org_unit_id, '')),
       toDateOrNull(toString(metric_date)),
       'git_output'                              AS section_id,
       metric_key                                AS series_key,
       sum(toFloat64OrNull(toString(metric_value)))
FROM insight.git_bullet_rows
WHERE metric_key IN ('commits','prs_merged')
GROUP BY person_id, org_unit_id, metric_date, metric_key

UNION ALL
-- code_quality: bugs_fixed + build_success daily
SELECT toString(person_id),
       toString(coalesce(org_unit_id, '')),
       toDateOrNull(toString(metric_date)),
       'code_quality'                            AS section_id,
       metric_key                                AS series_key,
       sum(toFloat64OrNull(toString(metric_value)))
FROM insight.code_quality_bullet_rows
WHERE metric_key IN ('bugs_fixed','build_success','pr_cycle_time')
GROUP BY person_id, org_unit_id, metric_date, metric_key

UNION ALL
-- ai_adoption: cursor_lines + cc_lines daily
SELECT toString(person_id),
       toString(coalesce(org_unit_id, '')),
       toDateOrNull(toString(metric_date)),
       'ai_adoption'                             AS section_id,
       metric_key                                AS series_key,
       sum(toFloat64OrNull(toString(metric_value)))
FROM insight.ai_bullet_rows
WHERE metric_key IN ('cursor_lines','cc_lines')
GROUP BY person_id, org_unit_id, metric_date, metric_key

UNION ALL
-- collaboration: meeting_hours + total_messages daily
SELECT toString(person_id),
       toString(coalesce(org_unit_id, '')),
       toDateOrNull(toString(metric_date)),
       'collaboration'                           AS section_id,
       'meeting_hours'                           AS series_key,
       sum(toFloat64OrNull(toString(metric_value)))
FROM insight.collab_bullet_rows
WHERE metric_key = 'meeting_hours'
GROUP BY person_id, org_unit_id, metric_date

UNION ALL
SELECT toString(person_id),
       toString(coalesce(org_unit_id, '')),
       toDateOrNull(toString(metric_date)),
       'collaboration'                           AS section_id,
       'total_messages'                          AS series_key,
       sum(toFloat64OrNull(toString(metric_value)))
FROM insight.collab_bullet_rows
WHERE metric_key IN ('slack_messages_sent','m365_emails_sent','m365_teams_chats')
GROUP BY person_id, org_unit_id, metric_date
;


-- ---------------------------------------------------------------------
-- insight.ic_kpi_peer_median — per-day KPI rows for peer-median computation
-- ---------------------------------------------------------------------
-- Long-format: one row per (person, kpi_key, metric_date) carrying the
-- daily KPI value + the person's supervisor_email. Catalog query_ref
-- (UUID 0…37) rolls these up per (supervisor, kpi_key) over the period
-- date filter and emits p25/p50/p75/n across the peer cohort.
--
-- Why a dedicated view (vs reusing peer_cohort_stats):
--   KPI metric_keys aren't all 1:1 with bullet_rows metric_keys
--   (tasks_closed vs tasks_completed, ai_loc_share vs ai_loc_share2,
--   focus_time_pct lives in ic_kpis only). This view sources directly
--   from insight.ic_kpis so the KPI value and its peer-median benchmark
--   are computed from the same data.
DROP VIEW IF EXISTS insight.ic_kpi_peer_median;
CREATE VIEW insight.ic_kpi_peer_median
AS
SELECT
    toString(k.person_id)                       AS person_id,
    p.supervisor_email                          AS supervisor_email,
    toDateOrNull(toString(k.metric_date))       AS metric_date,
    kpi.1                                       AS kpi_key,
    kpi.2                                       AS value
FROM insight.ic_kpis AS k
LEFT JOIN insight.people AS p
       ON p.person_id = k.person_id
ARRAY JOIN [
    ('bugs_fixed',      toFloat64(k.bugs_fixed)),
    ('tasks_closed',    toFloat64(k.tasks_closed)),
    ('prs_merged',      toFloat64(coalesce(k.prs_merged, 0))),
    ('ai_loc_share_pct', toFloat64(k.ai_loc_share_pct)),
    ('focus_time_pct',  toFloat64(k.focus_time_pct)),
    ('pr_cycle_time_h', toFloat64(coalesce(k.pr_cycle_time_h, 0))),
    ('ai_sessions',     toFloat64(k.ai_sessions))
] AS kpi
WHERE p.supervisor_email IS NOT NULL
  AND p.supervisor_email != ''
;
