-- ===================================================================
-- Vehicle Zone Intelligence — Database Schema
-- TimescaleDB (PostgreSQL 16 + extension)
-- ===================================================================

-- ===================================================================
-- CONFIGURATION TABLES
-- ===================================================================

CREATE TABLE edges (
    edge_id         TEXT PRIMARY KEY,
    hostname        TEXT,
    ip_address      INET,
    max_cameras     INT DEFAULT 35,
    last_heartbeat  TIMESTAMPTZ,
    status          TEXT DEFAULT 'unknown',
    meta            JSONB DEFAULT '{}'
);

CREATE TABLE cameras (
    camera_id       TEXT PRIMARY KEY,
    name            TEXT,
    source_url      TEXT NOT NULL,
    assigned_edge   TEXT REFERENCES edges(edge_id),
    status          TEXT DEFAULT 'unknown',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Zone groups: one logical zone covered by multiple cameras (e.g. "Zone 5"
-- visible from cameras 95.143 / 95.149 / 95.177 / 95.221). Aggregated count
-- = sum of member camera-zone counts. NULL group on `zones` keeps the
-- single-camera-zone model working unchanged.
CREATE TABLE zone_groups (
    group_id         TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    max_vehicles     INT,
    max_dwell_time_s REAL,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE zones (
    zone_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    camera_id       TEXT REFERENCES cameras(camera_id) ON UPDATE CASCADE,
    zone_group_id   TEXT REFERENCES zone_groups(group_id) ON DELETE SET NULL,
    zone_poly       JSONB,
    ramp_type       TEXT CHECK (ramp_type IN ('inner', 'outer')),
    max_vehicles    INT DEFAULT 20,
    max_dwell_time_s REAL DEFAULT 900.0,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_zones_group ON zones (zone_group_id) WHERE zone_group_id IS NOT NULL;

-- ===================================================================
-- TIME-SERIES DATA (TimescaleDB hypertables)
-- ===================================================================

CREATE TABLE vehicle_zone_metrics (
    ts                      TIMESTAMPTZ NOT NULL,
    zone_id                 TEXT NOT NULL,
    camera_id               TEXT,
    edge_id                 TEXT,
    vehicle_count           SMALLINT,
    vehicle_count_by_type   JSONB,
    occupancy_pct           REAL,
    overstay_count          SMALLINT,
    avg_dwell_time_s        REAL,
    max_dwell_time_s        REAL,
    total_entered           INTEGER,
    total_exited            INTEGER,
    overcrowding_alert      BOOLEAN,
    active_track_count      SMALLINT,
    inf_fps                 REAL,
    inf_ms                  REAL
);

SELECT create_hypertable('vehicle_zone_metrics', 'ts');
CREATE INDEX idx_vzm_zone_ts ON vehicle_zone_metrics (zone_id, ts DESC);

ALTER TABLE vehicle_zone_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'zone_id',
    timescaledb.compress_orderby = 'ts DESC'
);
SELECT add_compression_policy('vehicle_zone_metrics', INTERVAL '7 days');

-- 1-minute continuous aggregate
CREATE MATERIALIZED VIEW vehicle_zone_metrics_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts)         AS bucket,
    zone_id,
    AVG(vehicle_count)::REAL            AS avg_vehicle_count,
    MAX(vehicle_count)                  AS max_vehicle_count,
    AVG(occupancy_pct)::REAL            AS avg_occupancy_pct,
    AVG(avg_dwell_time_s)::REAL         AS avg_dwell_s,
    MAX(max_dwell_time_s)::REAL         AS max_dwell_s,
    AVG(overstay_count)::REAL           AS avg_overstay_count,
    MAX(total_entered)                  AS max_total_entered,
    MAX(total_exited)                   AS max_total_exited,
    BOOL_OR(overcrowding_alert)         AS had_overcrowding
FROM vehicle_zone_metrics
GROUP BY bucket, zone_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('vehicle_zone_metrics_1m',
    start_offset  => INTERVAL '10 minutes',
    end_offset    => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute'
);

-- 1-hour continuous aggregate (queries raw hypertable — PG14 compat)
CREATE MATERIALIZED VIEW vehicle_zone_metrics_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts)           AS bucket,
    zone_id,
    AVG(vehicle_count)::REAL            AS avg_vehicle_count,
    MAX(vehicle_count)                  AS max_vehicle_count,
    AVG(occupancy_pct)::REAL            AS avg_occupancy_pct,
    AVG(avg_dwell_time_s)::REAL         AS avg_dwell_s,
    MAX(max_dwell_time_s)::REAL         AS max_dwell_s,
    MAX(total_entered)                  AS max_total_entered,
    MAX(total_exited)                   AS max_total_exited,
    BOOL_OR(overcrowding_alert)         AS had_overcrowding
FROM vehicle_zone_metrics
GROUP BY bucket, zone_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('vehicle_zone_metrics_1h',
    start_offset  => INTERVAL '3 hours',
    end_offset    => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);

-- ===================================================================
-- ALERTS
-- ===================================================================

CREATE TABLE vehicle_alerts (
    alert_id        BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    zone_id         TEXT NOT NULL,
    camera_id       TEXT,
    edge_id         TEXT,
    alert_type      TEXT NOT NULL,   -- 'overcrowding' or 'overstay'
    level           TEXT NOT NULL,   -- 'warning' or 'critical'
    message         TEXT,
    vehicle_count   SMALLINT,
    dwell_time_s    REAL,
    track_id        INT,             -- NULL for overcrowding, set for overstay
    acknowledged    BOOLEAN DEFAULT FALSE,
    acked_by        TEXT,
    acked_at        TIMESTAMPTZ,
    image_url       TEXT             -- relative URL to evidence snapshot
);

CREATE INDEX idx_valerts_zone ON vehicle_alerts (zone_id, ts DESC);
CREATE INDEX idx_valerts_unacked ON vehicle_alerts (acknowledged, ts DESC) WHERE NOT acknowledged;

-- ===================================================================
-- SEED: Default edge device for POC
-- ===================================================================

INSERT INTO edges (edge_id, hostname, status) VALUES ('vehicle-edge-01', 'localhost', 'online');
