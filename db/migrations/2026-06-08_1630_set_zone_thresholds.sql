-- ============================================================================
-- 2026-06-08 — Set per-zone occupancy thresholds (max_vehicles) for the
--              OUTER / INNER ramp zones, as provided by operations.
-- ============================================================================
--
-- These are the vehicle-count caps shown on the Ramp Occupancy Map and used
-- for the calm/busy/over-capacity colour bands:
--
--     OUTER-1 = 0    INNER-1 = 0
--     OUTER-2 = 20   INNER-2 = 10
--     OUTER-3 = 10   INNER-3 = 7
--     OUTER-4 = 22   INNER-4 = 15
--     OUTER-5 = 16   INNER-5 = 10
--     OUTER-6 = 10   INNER-6 = 8
--
-- A logical zone's threshold lives in one of two places, matching how the
-- dashboard reads it (buildLogicalZones / renderThresholdSummary):
--   * grouped zones (multiple member cameras) -> zone_groups.max_vehicles
--   * standalone zones (no group)             -> zones.max_vehicles
--
-- We set BOTH, keyed by display name, with the standalone UPDATE guarded by
-- zone_group_id IS NULL so it never overwrites the per-camera caps of a
-- group's member zones. For a grouped label the zones UPDATE matches 0 rows;
-- for a standalone label the zone_groups UPDATE either matches an empty
-- like-named group (harmless) or 0 rows. Net effect: the rendered logical
-- threshold for each label is set correctly without side effects.
--
-- NOTE: a threshold of 0 (OUTER-1, INNER-1) means "over capacity" the moment
-- any vehicle is present (vehicle_count > max), i.e. zero-tolerance — these
-- cells show red with >=1 vehicle and green only when empty. This is the
-- value operations supplied; change to a positive cap if a soft limit was
-- intended instead.
--
-- Operational data (normally tuned from the dashboard UI); recorded here as a
-- migration for traceability and reproducibility across environments.
-- Idempotent: re-running simply re-applies the same values.
-- ============================================================================

\set ON_ERROR_STOP on
BEGIN;

-- OUTER ramp
UPDATE zone_groups SET max_vehicles = 0,  updated_at = NOW() WHERE name = 'OUTER-1';
UPDATE zones       SET max_vehicles = 0,  updated_at = NOW() WHERE name = 'OUTER-1' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 20, updated_at = NOW() WHERE name = 'OUTER-2';
UPDATE zones       SET max_vehicles = 20, updated_at = NOW() WHERE name = 'OUTER-2' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 10, updated_at = NOW() WHERE name = 'OUTER-3';
UPDATE zones       SET max_vehicles = 10, updated_at = NOW() WHERE name = 'OUTER-3' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 22, updated_at = NOW() WHERE name = 'OUTER-4';
UPDATE zones       SET max_vehicles = 22, updated_at = NOW() WHERE name = 'OUTER-4' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 16, updated_at = NOW() WHERE name = 'OUTER-5';
UPDATE zones       SET max_vehicles = 16, updated_at = NOW() WHERE name = 'OUTER-5' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 10, updated_at = NOW() WHERE name = 'OUTER-6';
UPDATE zones       SET max_vehicles = 10, updated_at = NOW() WHERE name = 'OUTER-6' AND zone_group_id IS NULL;

-- INNER ramp
UPDATE zone_groups SET max_vehicles = 0,  updated_at = NOW() WHERE name = 'INNER-1';
UPDATE zones       SET max_vehicles = 0,  updated_at = NOW() WHERE name = 'INNER-1' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 10, updated_at = NOW() WHERE name = 'INNER-2';
UPDATE zones       SET max_vehicles = 10, updated_at = NOW() WHERE name = 'INNER-2' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 7,  updated_at = NOW() WHERE name = 'INNER-3';
UPDATE zones       SET max_vehicles = 7,  updated_at = NOW() WHERE name = 'INNER-3' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 15, updated_at = NOW() WHERE name = 'INNER-4';
UPDATE zones       SET max_vehicles = 15, updated_at = NOW() WHERE name = 'INNER-4' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 10, updated_at = NOW() WHERE name = 'INNER-5';
UPDATE zones       SET max_vehicles = 10, updated_at = NOW() WHERE name = 'INNER-5' AND zone_group_id IS NULL;
UPDATE zone_groups SET max_vehicles = 8,  updated_at = NOW() WHERE name = 'INNER-6';
UPDATE zones       SET max_vehicles = 8,  updated_at = NOW() WHERE name = 'INNER-6' AND zone_group_id IS NULL;

COMMIT;
