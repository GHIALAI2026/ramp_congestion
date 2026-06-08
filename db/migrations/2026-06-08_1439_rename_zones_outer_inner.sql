-- ============================================================================
-- 2026-06-08 — Rename operator-facing zone names from "Zone <N>" to the
--              OUTER-<N> / INNER-<N> ramp-side scheme.
-- ============================================================================
--
-- Operators now refer to the two arrival-ramp rows as OUTER and INNER rather
-- than the flat "Zone 1..12" numbering. The floor plan on the Overview page
-- already lays the zones out in two rows; this migration relabels them so the
-- on-screen names match how the control room talks about them:
--
--     Top row (outer ramp):     Zone 1..Zone 6   -> OUTER-1..OUTER-6
--     Bottom row (inner ramp):  Zone 12..Zone 7  -> INNER-1..INNER-6
--
-- Note the inner row is renumbered in REVERSE: Zone 12 becomes INNER-1 and
-- Zone 7 becomes INNER-6, matching the left-to-right reading order operators
-- use when facing the ramp. This is the mapping the operator confirmed:
--
--     Zone 1  -> OUTER-1      Zone 12 -> INNER-1
--     Zone 2  -> OUTER-2      Zone 11 -> INNER-2
--     Zone 3  -> OUTER-3      Zone 10 -> INNER-3
--     Zone 4  -> OUTER-4      Zone 9  -> INNER-4
--     Zone 5  -> OUTER-5      Zone 8  -> INNER-5
--     Zone 6  -> OUTER-6      Zone 7  -> INNER-6
--
-- This is a DISPLAY-NAME-only change. It touches zone_groups.name and
-- zones.name (the human labels shown on the dashboard, floor plan, and alert
-- list). The technical identifiers — zones.zone_id, zone_groups.group_id, and
-- every downstream zone_id reference in vehicle_zone_metrics (42M+ rows, mostly
-- in compressed TimescaleDB chunks), the continuous aggregates, vehicle_alerts,
-- the Redis vzone:* keys, and the edge-agent zone configs — are deliberately
-- left UNCHANGED. Operators never see zone_id, so renaming it would rewrite the
-- entire metrics history for no visible benefit. Because no identifier moves,
-- no Redis purge and no edge-agent restart are required: the cloud API serves
-- names straight from these tables, so a dashboard refresh picks them up.
--
-- Matching is case-insensitive on the trimmed name (the live data mixes
-- "Zone 10" and "zone 10"); the rewrite also normalises the casing. The new
-- values (OUTER-*/INNER-*) never match the old "zone <n>" pattern, so
-- re-running this migration is a no-op — it is idempotent.
-- ============================================================================

\set ON_ERROR_STOP on
BEGIN;

-- --- Outer ramp (top row): Zone 1..6 -> OUTER-1..6 -------------------------
UPDATE zones        SET name = 'OUTER-1' WHERE lower(trim(name)) = 'zone 1';
UPDATE zone_groups  SET name = 'OUTER-1' WHERE lower(trim(name)) = 'zone 1';

UPDATE zones        SET name = 'OUTER-2' WHERE lower(trim(name)) = 'zone 2';
UPDATE zone_groups  SET name = 'OUTER-2' WHERE lower(trim(name)) = 'zone 2';

UPDATE zones        SET name = 'OUTER-3' WHERE lower(trim(name)) = 'zone 3';
UPDATE zone_groups  SET name = 'OUTER-3' WHERE lower(trim(name)) = 'zone 3';

UPDATE zones        SET name = 'OUTER-4' WHERE lower(trim(name)) = 'zone 4';
UPDATE zone_groups  SET name = 'OUTER-4' WHERE lower(trim(name)) = 'zone 4';

UPDATE zones        SET name = 'OUTER-5' WHERE lower(trim(name)) = 'zone 5';
UPDATE zone_groups  SET name = 'OUTER-5' WHERE lower(trim(name)) = 'zone 5';

UPDATE zones        SET name = 'OUTER-6' WHERE lower(trim(name)) = 'zone 6';
UPDATE zone_groups  SET name = 'OUTER-6' WHERE lower(trim(name)) = 'zone 6';

-- --- Inner ramp (bottom row, REVERSED): Zone 12..7 -> INNER-1..6 ------------
UPDATE zones        SET name = 'INNER-1' WHERE lower(trim(name)) = 'zone 12';
UPDATE zone_groups  SET name = 'INNER-1' WHERE lower(trim(name)) = 'zone 12';

UPDATE zones        SET name = 'INNER-2' WHERE lower(trim(name)) = 'zone 11';
UPDATE zone_groups  SET name = 'INNER-2' WHERE lower(trim(name)) = 'zone 11';

UPDATE zones        SET name = 'INNER-3' WHERE lower(trim(name)) = 'zone 10';
UPDATE zone_groups  SET name = 'INNER-3' WHERE lower(trim(name)) = 'zone 10';

UPDATE zones        SET name = 'INNER-4' WHERE lower(trim(name)) = 'zone 9';
UPDATE zone_groups  SET name = 'INNER-4' WHERE lower(trim(name)) = 'zone 9';

UPDATE zones        SET name = 'INNER-5' WHERE lower(trim(name)) = 'zone 8';
UPDATE zone_groups  SET name = 'INNER-5' WHERE lower(trim(name)) = 'zone 8';

UPDATE zones        SET name = 'INNER-6' WHERE lower(trim(name)) = 'zone 7';
UPDATE zone_groups  SET name = 'INNER-6' WHERE lower(trim(name)) = 'zone 7';

-- Bust the per-row cache by touching updated_at on every renamed row, so a
-- client polling the zones/groups list gets the fresh labels on its next tick.
UPDATE zones        SET updated_at = NOW() WHERE name ~ '^(OUTER|INNER)-[0-9]+$';
UPDATE zone_groups  SET updated_at = NOW() WHERE name ~ '^(OUTER|INNER)-[0-9]+$';

COMMIT;
