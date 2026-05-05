{{ config(
    materialized='view',
    schema='staging',
    tags=['zoom', 'silver:class_collab_meeting_activity']
) }}

-- Materialized as a view (not incremental) because:
--   1. The session-stitching CTE chain (issue #258) needs visibility into
--      the full meeting history per Zoom Meeting ID to correctly assign
--      cluster_idx — an incremental window over participants would cluster
--      against an outdated meetings snapshot and miss-stitch edge cases at
--      the window boundary.
--   2. dbt-clickhouse incremental materialization does not currently
--      support top-level WITH clauses (the wrapped SQL produces
--      "Unmatched parentheses" at compile time).
-- The downstream Silver model class_collab_meeting_activity is itself
-- incremental on _version, so view recomputation cost is bounded.

-- Zoom meeting activity aggregated per user per day.
--
-- Grain: (tenant, source, email, date). We intentionally filter out participants
-- without an email (guests / anonymous joiners) because:
--   1. Without a stable user identifier, a COALESCE(email, user_name) key is
--      unstable — the same person can flip keys between batches depending on
--      whether Zoom returns their email that run.
--   2. Anonymous participants can't be joined to identity at the Silver layer
--      anyway, so they add noise without enabling any downstream use case.
-- If Zoom ever starts exposing a stable participant_id/user_id, switch to that.
--
-- Meeting-session stitching (issue #258):
-- Zoom assigns a NEW `uuid` to every meeting session, but the numeric `id`
-- (Zoom Meeting ID) is reused — both for legitimate recurring meetings AND
-- when the host disconnects and reconnects (host drop, network blip, etc).
-- A naive `count(*)` over participant rows therefore over-reports
-- `meetings_attended` whenever a host drops off, since each user gets one
-- participant row per session of the same logical meeting.
--
-- We stitch sessions sharing the same `(tenant, source, id)` whose
-- end-of-prev → start-of-next gap is ≤ `session_gap_seconds` (15 min) into
-- a single `logical_meeting_id`. Recurring meetings that legitimately reuse
-- an `id` end up in different clusters because their gaps are hours/days,
-- not minutes. `meetings_attended` then counts distinct logical meetings
-- per user per day instead of distinct participant rows.
--
-- Durations (`audio_duration_seconds`, `video_duration_seconds`,
-- `screen_share_duration_seconds`) are NOT affected — they sum real
-- per-participant join/leave intervals, which are correct to add across
-- split sessions (the user really was in-call during both halves).

{% set session_gap_seconds = 900 %}

WITH meetings_dedup AS (
    -- Drop bronze re-emit duplicates (same uuid emitted multiple times by
    -- Airbyte): keep the latest extraction. Without this, the same session
    -- can land in two adjacent clusters and skew the stitching. ClickHouse
    -- doesn't support QUALIFY here (CTE-in-CTE context), so we use the
    -- equivalent `LIMIT 1 BY uuid` pattern over an inner subquery.
    SELECT
        tenant_id,
        source_id,
        toString(id)                                             AS id,
        uuid,
        parseDateTimeBestEffortOrNull(coalesce(start_time, ''))  AS start_ts,
        parseDateTimeBestEffortOrNull(coalesce(end_time, ''))    AS end_ts,
        has_video,
        has_screen_share
    FROM (
        SELECT *
        FROM {{ source('bronze_zoom', 'meetings') }}
        WHERE id IS NOT NULL
          AND uuid IS NOT NULL AND uuid != ''
        ORDER BY _airbyte_extracted_at DESC
        LIMIT 1 BY uuid
    ) AS deduped
),

meetings_with_gap AS (
    -- gap_seconds = seconds since end of the previous session of the SAME
    -- (tenant, source, id), ordered by start_ts. NULL for the first session
    -- of each (tenant, source, id) chain.
    SELECT
        *,
        dateDiff(
            'second',
            lagInFrame(end_ts) OVER (
                PARTITION BY tenant_id, source_id, id
                ORDER BY start_ts
            ),
            start_ts
        ) AS gap_seconds
    FROM meetings_dedup
    WHERE start_ts IS NOT NULL
),

meetings_clustered AS (
    -- cluster_idx is a running counter within (tenant, source, id):
    -- increments on the first session and on every gap > threshold.
    -- logical_meeting_id is unique within (tenant, source) — we don't need
    -- it globally unique because the participants JOIN already pins it to a
    -- specific meeting via uuid.
    SELECT
        tenant_id,
        source_id,
        id,
        uuid,
        has_video,
        has_screen_share,
        sum(CASE WHEN gap_seconds IS NULL OR gap_seconds > {{ session_gap_seconds }} THEN 1 ELSE 0 END)
            OVER (
                PARTITION BY tenant_id, source_id, id
                ORDER BY start_ts
            ) AS cluster_idx
    FROM meetings_with_gap
),

meetings_logical AS (
    SELECT
        tenant_id,
        source_id,
        uuid,
        has_video,
        has_screen_share,
        concat(id, '#', toString(cluster_idx)) AS logical_meeting_id
    FROM meetings_clustered
)

SELECT
    p.tenant_id,
    p.source_id AS insight_source_id,
    MD5(concat(
        p.tenant_id, '-',
        p.source_id, '-',
        lower(p.email), '-',
        toString(toDate(parseDateTimeBestEffort(p.join_time)))
    )) AS unique_key,
    p.email AS user_id,
    -- Pick one display name when the same email surfaces under multiple
    -- spellings (e.g., "Karolis Dambrava" vs "karolisdambrava"). Without
    -- this, GROUP BY would split them and produce two rows with identical
    -- unique_key — the staging model's `unique_key` is keyed on
    -- (tenant, source, lower(email), date), so user_name is non-keying.
    coalesce(any(p.user_name), '') AS user_name,
    p.email AS email,
    lower(p.email) AS person_key,
    toDate(parseDateTimeBestEffort(p.join_time)) AS date,
    CAST(NULL AS Nullable(Int64)) AS calls_count,
    CAST(NULL AS Nullable(Int64)) AS meetings_organized,
    -- uniqExact over logical_meeting_id collapses host-drop rejoins into one.
    -- Falls back to participant.meeting_uuid when the JOIN miss (meeting row
    -- not yet synced) — preserves "one row → one meeting" behavior consistent
    -- with the previous count(*) for unstitched data.
    toInt64(uniqExact(coalesce(ml.logical_meeting_id, p.meeting_uuid))) AS meetings_attended,
    CAST(NULL AS Nullable(Int64)) AS adhoc_meetings_organized,
    CAST(NULL AS Nullable(Int64)) AS adhoc_meetings_attended,
    CAST(NULL AS Nullable(Int64)) AS scheduled_meetings_organized,
    CAST(NULL AS Nullable(Int64)) AS scheduled_meetings_attended,
    toInt64(sum(
        if(p.join_time IS NOT NULL AND p.leave_time IS NOT NULL,
           dateDiff('second', parseDateTimeBestEffort(p.join_time), parseDateTimeBestEffort(p.leave_time)),
           0)
    )) AS audio_duration_seconds,
    toInt64(sumIf(
        if(p.join_time IS NOT NULL AND p.leave_time IS NOT NULL,
           dateDiff('second', parseDateTimeBestEffort(p.join_time), parseDateTimeBestEffort(p.leave_time)),
           0),
        ml.has_video = true
    )) AS video_duration_seconds,
    toInt64(sumIf(
        if(p.join_time IS NOT NULL AND p.leave_time IS NOT NULL,
           dateDiff('second', parseDateTimeBestEffort(p.join_time), parseDateTimeBestEffort(p.leave_time)),
           0),
        ml.has_screen_share = true
    )) AS screen_share_duration_seconds,
    CAST(NULL AS Nullable(String)) AS report_period,
    now() AS collected_at,
    'insight_zoom' AS data_source,
    toUnixTimestamp64Milli(now64()) AS _version
FROM {{ source('bronze_zoom', 'participants') }} p
LEFT JOIN meetings_logical ml
    ON p.meeting_uuid = ml.uuid
    AND p.tenant_id = ml.tenant_id
    AND p.source_id = ml.source_id
WHERE p.join_time IS NOT NULL
  AND p.email IS NOT NULL
  AND p.email != ''
GROUP BY
    p.tenant_id,
    p.source_id,
    p.email,
    toDate(parseDateTimeBestEffort(p.join_time))
