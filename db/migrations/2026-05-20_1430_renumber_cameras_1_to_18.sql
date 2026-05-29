-- ============================================================================
-- 2026-05-20 — Renumber cameras sequentially 1..18 (close numbering gaps).
-- ============================================================================
--
-- The fleet stabilised at 18 active cameras but the camera_id and zone_id
-- numbering still carried gaps from the bring-up period (2..11, 13..16,
-- 18, 19, 25, and the legacy "Cam_26"). Operators looking at an alert
-- "cam 19" had to remember whether 19 meant the 16th camera in the
-- dropdown or something else; the Live View dropdown likewise listed a
-- non-contiguous sequence. Closing the gaps to a clean 1..18 makes
-- alert→Live-View lookup a direct index match.
--
-- The actual rename was applied against the live DB on 2026-05-20.
-- This commit captures it as a reproducible, atomic SQL script for any
-- other deployment that started from the same pre-renumber state.
--
-- Steps:
--   1. Switch zones.camera_id FK to ON UPDATE CASCADE so PK renames on
--      cameras auto-propagate. This becomes the new permanent behaviour
--      (also reflected in schema.sql) — future renames don't need a
--      drop/recreate dance.
--   2. Rename cameras in ascending source order so each step's new_id
--      slot was freed by the previous step (cam_2→cam_1 frees cam_2,
--      then cam_3→cam_2, etc.) — no temp-prefix needed.
--   3. Mirror camera_id renames in vehicle_alerts + vehicle_zone_metrics
--      (text columns, no FK).
--   4. Same shift for zones.zone_id; the multi-zone speed-breaker
--      camera keeps its a/b suffix: arrival_zone_18a/b → arrival_zone_15a/b.
--   5. Rewrite cameras.name to "cam <N>" matching the new camera_id.
--
-- After running, also purge stale Redis keys whose zone no longer
-- exists in the DB, and SIGKILL the edge so the supervisor relaunches
-- it with fresh zone configs from the API.
--
-- Idempotent: each UPDATE's WHERE clause matches the OLD value, so
-- re-running on an already-migrated database is a no-op.
-- ============================================================================

\set ON_ERROR_STOP on
BEGIN;

-- 1. Make zones.camera_id FK cascade renames. Future PK renames on
-- cameras no longer need a drop/recreate dance.
ALTER TABLE zones DROP CONSTRAINT zones_camera_id_fkey;
ALTER TABLE zones ADD CONSTRAINT zones_camera_id_fkey
    FOREIGN KEY (camera_id) REFERENCES cameras(camera_id) ON UPDATE CASCADE;

-- 2. Camera renames, in ascending-source order so each step frees the
-- next target slot (cam_2→cam_1 frees cam_2, then cam_3→cam_2, etc.).
-- zones.camera_id auto-cascades; vehicle_alerts/vehicle_zone_metrics
-- need explicit updates since they have no FK.
UPDATE cameras SET camera_id = 'arrival_cam_1'  WHERE camera_id = 'arrival_cam_2';
UPDATE cameras SET camera_id = 'arrival_cam_2'  WHERE camera_id = 'arrival_cam_3';
UPDATE cameras SET camera_id = 'arrival_cam_3'  WHERE camera_id = 'arrival_cam_4';
UPDATE cameras SET camera_id = 'arrival_cam_4'  WHERE camera_id = 'arrival_cam_5';
UPDATE cameras SET camera_id = 'arrival_cam_5'  WHERE camera_id = 'arrival_cam_6';
UPDATE cameras SET camera_id = 'arrival_cam_6'  WHERE camera_id = 'arrival_cam_7';
UPDATE cameras SET camera_id = 'arrival_cam_7'  WHERE camera_id = 'arrival_cam_8';
UPDATE cameras SET camera_id = 'arrival_cam_8'  WHERE camera_id = 'arrival_cam_9';
UPDATE cameras SET camera_id = 'arrival_cam_9'  WHERE camera_id = 'arrival_cam_10';
UPDATE cameras SET camera_id = 'arrival_cam_10' WHERE camera_id = 'arrival_cam_11';
UPDATE cameras SET camera_id = 'arrival_cam_11' WHERE camera_id = 'arrival_cam_13';
UPDATE cameras SET camera_id = 'arrival_cam_12' WHERE camera_id = 'arrival_cam_14';
UPDATE cameras SET camera_id = 'arrival_cam_13' WHERE camera_id = 'arrival_cam_15';
UPDATE cameras SET camera_id = 'arrival_cam_14' WHERE camera_id = 'arrival_cam_16';
UPDATE cameras SET camera_id = 'arrival_cam_15' WHERE camera_id = 'arrival_cam_18';
UPDATE cameras SET camera_id = 'arrival_cam_16' WHERE camera_id = 'arrival_cam_19';
UPDATE cameras SET camera_id = 'arrival_cam_17' WHERE camera_id = 'arrival_cam_25';
UPDATE cameras SET camera_id = 'arrival_cam_18' WHERE camera_id = 'Cam_26';

-- 3. Mirror the renames in history tables. Use the same ascending order
-- — each statement keys off the OLD id so they don't interfere.
DO $$
DECLARE
    pairs text[][] := ARRAY[
        ['arrival_cam_2',  'arrival_cam_1'],
        ['arrival_cam_3',  'arrival_cam_2'],
        ['arrival_cam_4',  'arrival_cam_3'],
        ['arrival_cam_5',  'arrival_cam_4'],
        ['arrival_cam_6',  'arrival_cam_5'],
        ['arrival_cam_7',  'arrival_cam_6'],
        ['arrival_cam_8',  'arrival_cam_7'],
        ['arrival_cam_9',  'arrival_cam_8'],
        ['arrival_cam_10', 'arrival_cam_9'],
        ['arrival_cam_11', 'arrival_cam_10'],
        ['arrival_cam_13', 'arrival_cam_11'],
        ['arrival_cam_14', 'arrival_cam_12'],
        ['arrival_cam_15', 'arrival_cam_13'],
        ['arrival_cam_16', 'arrival_cam_14'],
        ['arrival_cam_18', 'arrival_cam_15'],
        ['arrival_cam_19', 'arrival_cam_16'],
        ['arrival_cam_25', 'arrival_cam_17'],
        ['Cam_26',         'arrival_cam_18']
    ];
    p text[];
BEGIN
    FOREACH p SLICE 1 IN ARRAY pairs LOOP
        EXECUTE 'UPDATE vehicle_alerts SET camera_id = $1 WHERE camera_id = $2'
            USING p[2], p[1];
        EXECUTE 'UPDATE vehicle_zone_metrics SET camera_id = $1 WHERE camera_id = $2'
            USING p[2], p[1];
    END LOOP;
END $$;

-- 4. Zone renames — same ascending pattern. arrival_zone_18a/b carry
-- their letters through to the new arrival_zone_15a/b.
UPDATE zones SET zone_id = 'arrival_zone_1'   WHERE zone_id = 'arrival_zone_2';
UPDATE zones SET zone_id = 'arrival_zone_2'   WHERE zone_id = 'arrival_zone_3';
UPDATE zones SET zone_id = 'arrival_zone_3'   WHERE zone_id = 'arrival_zone_4';
UPDATE zones SET zone_id = 'arrival_zone_4'   WHERE zone_id = 'arrival_zone_5';
UPDATE zones SET zone_id = 'arrival_zone_5'   WHERE zone_id = 'arrival_zone_6';
UPDATE zones SET zone_id = 'arrival_zone_6'   WHERE zone_id = 'arrival_zone_7';
UPDATE zones SET zone_id = 'arrival_zone_7'   WHERE zone_id = 'arrival_zone_8';
UPDATE zones SET zone_id = 'arrival_zone_8'   WHERE zone_id = 'arrival_zone_9';
UPDATE zones SET zone_id = 'arrival_zone_9'   WHERE zone_id = 'arrival_zone_10';
UPDATE zones SET zone_id = 'arrival_zone_10'  WHERE zone_id = 'arrival_zone_11';
UPDATE zones SET zone_id = 'arrival_zone_11'  WHERE zone_id = 'arrival_zone_13';
UPDATE zones SET zone_id = 'arrival_zone_12'  WHERE zone_id = 'arrival_zone_14';
UPDATE zones SET zone_id = 'arrival_zone_13'  WHERE zone_id = 'arrival_zone_15';
UPDATE zones SET zone_id = 'arrival_zone_14'  WHERE zone_id = 'arrival_zone_16';
UPDATE zones SET zone_id = 'arrival_zone_15a' WHERE zone_id = 'arrival_zone_18a';
UPDATE zones SET zone_id = 'arrival_zone_15b' WHERE zone_id = 'arrival_zone_18b';
UPDATE zones SET zone_id = 'arrival_zone_16'  WHERE zone_id = 'arrival_zone_19';
UPDATE zones SET zone_id = 'arrival_zone_17'  WHERE zone_id = 'arrival_zone_25';
UPDATE zones SET zone_id = 'arrival_zone_18'  WHERE zone_id = 'arrival_zone_26';

DO $$
DECLARE
    pairs text[][] := ARRAY[
        ['arrival_zone_2',   'arrival_zone_1'],
        ['arrival_zone_3',   'arrival_zone_2'],
        ['arrival_zone_4',   'arrival_zone_3'],
        ['arrival_zone_5',   'arrival_zone_4'],
        ['arrival_zone_6',   'arrival_zone_5'],
        ['arrival_zone_7',   'arrival_zone_6'],
        ['arrival_zone_8',   'arrival_zone_7'],
        ['arrival_zone_9',   'arrival_zone_8'],
        ['arrival_zone_10',  'arrival_zone_9'],
        ['arrival_zone_11',  'arrival_zone_10'],
        ['arrival_zone_13',  'arrival_zone_11'],
        ['arrival_zone_14',  'arrival_zone_12'],
        ['arrival_zone_15',  'arrival_zone_13'],
        ['arrival_zone_16',  'arrival_zone_14'],
        ['arrival_zone_18a', 'arrival_zone_15a'],
        ['arrival_zone_18b', 'arrival_zone_15b'],
        ['arrival_zone_19',  'arrival_zone_16'],
        ['arrival_zone_25',  'arrival_zone_17'],
        ['arrival_zone_26',  'arrival_zone_18']
    ];
    p text[];
BEGIN
    FOREACH p SLICE 1 IN ARRAY pairs LOOP
        EXECUTE 'UPDATE vehicle_alerts SET zone_id = $1 WHERE zone_id = $2'
            USING p[2], p[1];
        EXECUTE 'UPDATE vehicle_zone_metrics SET zone_id = $1 WHERE zone_id = $2'
            USING p[2], p[1];
    END LOOP;
END $$;

-- 5. Refresh display name to match new camera number, and stamp updated_at
-- so any client cache busts on the next fetch.
UPDATE cameras
SET name = 'cam ' || regexp_replace(camera_id, '.*_(\d+)$', '\1'),
    updated_at = NOW();

UPDATE zones SET updated_at = NOW();

COMMIT;
