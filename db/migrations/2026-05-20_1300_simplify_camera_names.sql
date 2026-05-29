-- ============================================================================
-- 2026-05-20 — Simplify cameras.name to "cam <N>".
-- ============================================================================
--
-- The cameras.name column held free-text labels operators had typed in
-- during install ("Arrival ramp .157", "Arrival - New Ramp .141",
-- "Arrival ramp 5 177"). All of those names already embedded the
-- camera's IP suffix or a similar identifier; the camera_id itself
-- (arrival_cam_N) ended with the same number; the alerts page and the
-- Live View dropdown both used the longer label as the display string
-- and forced operators to scan a paragraph of text to find the camera
-- they wanted.
--
-- Replace every name with "cam <N>" where N is the trailing number on
-- the camera_id. So arrival_cam_5 displays as "cam 5", Cam_26 displays
-- as "cam 26", etc. Spotting the right camera in the dropdown when
-- triaging an alert ("alert is on cam 5") becomes a 1-character match
-- instead of a name-vs-IP cross-reference.
--
-- Only `name` changes — camera_id (the PK) is untouched, so no FK
-- references, alerts, zones, or edge configs need to be migrated. The
-- name is a pure display label.
--
-- Idempotent: each row's WHERE clause matches camera_ids that end with
-- "_<digits>", so re-running on an already-migrated database just
-- rewrites the name to the same value. The previous arbitrary labels
-- are gone after the first run.
-- ============================================================================

\set ON_ERROR_STOP on
BEGIN;

UPDATE cameras
SET name = 'cam ' || regexp_replace(camera_id, '.*[_](\d+)$', '\1'),
    updated_at = NOW()
WHERE camera_id ~ '_\d+$';

COMMIT;
