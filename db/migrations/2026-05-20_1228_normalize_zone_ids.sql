-- ============================================================================
-- 2026-05-20 — Normalize zone_id values to a single `arrival_zone_<N>` scheme.
-- ============================================================================
--
-- The zones table accumulated inconsistent zone_id values during early
-- bring-up: some matched the camera-numbering scheme (arrival_zone_3),
-- others were ad-hoc labels operators typed in the editor ("Zone 41",
-- "Zone 152", "Zone-07", "Zone 6", "Zone 172", "Zone 177"). With group-
-- level alerts now in play and an operator-facing list of cameras
-- numbered cam_2..cam_26, the inconsistency made cross-referencing the
-- alerts page against the camera config harder than it needed to be.
--
-- This migration normalises every zone_id to:
--   * arrival_zone_<N>       for single-zone cameras (N = camera number)
--   * arrival_zone_<N>a / b  for the speed-breaker camera (arrival_cam_18)
--     which carries two physical zones split by the breaker stripes.
--
-- Each block updates zones (the PK), then propagates the rename to
-- vehicle_alerts.zone_id and vehicle_zone_metrics.zone_id so alert
-- history and time-series charts continue to resolve under the new
-- identifiers. Both downstream tables store zone_id as plain text
-- (no FK constraint), so update order doesn't matter.
--
-- After running, also purge stale Redis keys (vzone:<old_id>:latest and
-- vzone:entry:<old_id>:*) — they don't auto-expire and would otherwise
-- clutter Redis indefinitely — and SIGKILL the edge agent so the
-- supervisor relaunches it with fresh zone configs from the API.
--
-- Idempotent: each WHERE clause matches the OLD value, so re-running on
-- an already-migrated database is a no-op.
-- ============================================================================

\set ON_ERROR_STOP on
BEGIN;

UPDATE zones                 SET zone_id = 'arrival_zone_2'   WHERE zone_id = 'Zone 41';
UPDATE vehicle_alerts        SET zone_id = 'arrival_zone_2'   WHERE zone_id = 'Zone 41';
UPDATE vehicle_zone_metrics  SET zone_id = 'arrival_zone_2'   WHERE zone_id = 'Zone 41';

UPDATE zones                 SET zone_id = 'arrival_zone_8'   WHERE zone_id = 'Zone 152';
UPDATE vehicle_alerts        SET zone_id = 'arrival_zone_8'   WHERE zone_id = 'Zone 152';
UPDATE vehicle_zone_metrics  SET zone_id = 'arrival_zone_8'   WHERE zone_id = 'Zone 152';

UPDATE zones                 SET zone_id = 'arrival_zone_11'  WHERE zone_id = 'Zone 172';
UPDATE vehicle_alerts        SET zone_id = 'arrival_zone_11'  WHERE zone_id = 'Zone 172';
UPDATE vehicle_zone_metrics  SET zone_id = 'arrival_zone_11'  WHERE zone_id = 'Zone 172';

UPDATE zones                 SET zone_id = 'arrival_zone_16'  WHERE zone_id = 'Zone-07';
UPDATE vehicle_alerts        SET zone_id = 'arrival_zone_16'  WHERE zone_id = 'Zone-07';
UPDATE vehicle_zone_metrics  SET zone_id = 'arrival_zone_16'  WHERE zone_id = 'Zone-07';

-- arrival_cam_18 has TWO zones split by a speed breaker; suffix a/b so
-- both halves follow the same naming convention as the rest of the fleet.
UPDATE zones                 SET zone_id = 'arrival_zone_18a' WHERE zone_id = 'arrival_zone_18';
UPDATE vehicle_alerts        SET zone_id = 'arrival_zone_18a' WHERE zone_id = 'arrival_zone_18';
UPDATE vehicle_zone_metrics  SET zone_id = 'arrival_zone_18a' WHERE zone_id = 'arrival_zone_18';

UPDATE zones                 SET zone_id = 'arrival_zone_18b' WHERE zone_id = 'Zone 6';
UPDATE vehicle_alerts        SET zone_id = 'arrival_zone_18b' WHERE zone_id = 'Zone 6';
UPDATE vehicle_zone_metrics  SET zone_id = 'arrival_zone_18b' WHERE zone_id = 'Zone 6';

UPDATE zones                 SET zone_id = 'arrival_zone_26'  WHERE zone_id = 'Zone 177';
UPDATE vehicle_alerts        SET zone_id = 'arrival_zone_26'  WHERE zone_id = 'Zone 177';
UPDATE vehicle_zone_metrics  SET zone_id = 'arrival_zone_26'  WHERE zone_id = 'Zone 177';

-- Bust the per-row cache by touching updated_at on every renamed row,
-- so a client polling the zones list gets the fresh state on its next tick.
UPDATE zones SET updated_at = NOW()
WHERE zone_id IN (
    'arrival_zone_2',  'arrival_zone_8',  'arrival_zone_11',
    'arrival_zone_16', 'arrival_zone_18a','arrival_zone_18b',
    'arrival_zone_26'
);

COMMIT;
