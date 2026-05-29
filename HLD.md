---
title: Vehicle Zone Intelligence — High-Level Design
version: 1.0
date: 2026-05-04
status: For review
audience: Engineering, IT Operations, External Reviewers
scope: Production architecture (Edge CV + Cloud API + Dashboard)
---

# Vehicle Zone Intelligence — High-Level Design

## 1. Executive summary

Vehicle Zone Intelligence is a real-time computer vision platform designed to monitor vehicle traffic, dwell times, and occupancy across designated physical zones. It processes live CCTV feeds at the edge, tracks vehicle movement, and streams telemetry to a central on-premise server that provides real-time operational visibility and automated alerting for overstays and overcrowding.

The product owns three primary components end-to-end: the edge computer-vision pipeline running on specialized hardware, the central API and database subsystem that aggregates zone metrics, and the real-time operations dashboard. Edge agents sit on the local network near the cameras to handle the intensive video streams and minimize bandwidth usage; the aggregated metadata is then pushed to the central on-premise environment.

This document serves as the production High-Level Design (HLD) for the Vehicle Zone Intelligence system.

## 2. Goals and non-goals

### Goals

- **Live measurement** of vehicle counts, zone occupancy percentages, and real-time active tracks, refreshing seamlessly on the dashboard.
- **Dwell time tracking** for every individual vehicle, calculating precise entry and exit times, average dwell, and maximum dwell times per zone.
- **Overstay and overcrowding alerts** generated at the edge and propagated to the central server when vehicles wait too long or zones exceed capacity.
- **Vehicle type classification** (e.g., car, truck, bus) to provide a breakdown of the vehicles occupying a zone.
- **Resilient edge-to-server telemetry** using MQTT with QoS 1.
- **Operations dashboard** powered by a real-time WebSocket feed to provide an instant overview of all monitored zones.

### Non-goals

- Public-cloud video ingest. Video streams remain strictly on the local edge network; only metadata and events leave the premises.
- Long-term cross-camera vehicle Re-Identification (Re-ID). Tracking is scoped to the individual camera's field of view to measure zone occupancy and dwell time.
- Automated ticketing or license plate recognition (ALPR/ANPR) unless explicitly integrated as a separate module.

### Constraints

- **Edge proximity.** Edge agents must sit on the local network to ingest RTSP streams directly without saturating external internet links.
- **Hardware (edge).** Axelera Metis AIPU. Edge processing leverages the Voyager SDK for hardware-accelerated decode and inference.
- **Data volume.** Sub-second vehicle telemetry necessitates efficient time-series storage (TimescaleDB) and in-memory caching (Redis) to prevent database bottlenecking.

## 3. Glossary

| Term | Meaning |
|---|---|
| **Edge Box** | Server hardware running the `edge_agent`, equipped with an Axelera accelerator. |
| **Central Server** | On-premise server hosting the FastAPI application, Redis, and TimescaleDB. |
| **Zone** | A defined polygon within a camera's frame; vehicles inside it are tracked and counted. |
| **Dwell Time** | The duration a specific vehicle has remained continuously inside a designated zone. |
| **Voyager SDK** | Axelera runtime utilized for efficient video decode and INT8 neural network inference. |
| **Deep OC-SORT** | Object tracking algorithm combining OC-SORT's motion models with a deep Re-ID embedding for resilient tracking. |
| **TimescaleDB** | PostgreSQL extension used for efficient time-series data storage and continuous aggregation. |

---

# Part I: Architecture overview

## 4. System context

```text
              Edge (Local Network)                              Central On-Premise Server
        ┌─────────────────────────────┐                ┌─────────────────────────────────────────┐
        │  CCTV Cameras               │                │  MQTT Broker                            │
        │  (Parking, loading bays,    │                │       │                                 │
        │   drop-off zones)           │                │       ▼                                 │
        └────────────┬────────────────┘                │  FastAPI Central Server: server/main.py │
                     │ RTSP (TCP 554)                  │   ├─ MQTT Consumer (Telemetry)         │
                     ▼                                 │   ├─ Alert Engine                      │
        ┌──────────────────────────┐                   │   └─ API & WebSocket Gateway           │
        │ Edge Agent Node          │   MQTT/TLS  ────▶ │       │                                 │
        │ Voyager SDK Inference    │   :8883           │       ├─▶ PostgreSQL + TimescaleDB      │
        │ VehicleZoneAnalytics     │                   │       │      (Metrics, Alerts, Config)  │
        │ MQTT Publisher           │                   │       └─▶ Redis (Replica/Pub-Sub)       │
        └──────────────────────────┘                   │              (Live state fan-out)       │
                                                       └─────────────────────┬───────────────────┘
                                                                             │ HTTPS + WebSocket
                                                                             ▼
                                                              ┌─────────────────────┐
                                                              │  React Dashboard    │
                                                              │  (Live Monitoring)  │
                                                              └─────────────────────┘
```

## 5. Logical architecture

The system operates across three primary layers: the edge AI agents, the central on-premise backend, and the frontend dashboard.

### 5.1 Edge agents

A Python application utilizing the Axelera Voyager SDK (`edge_agent/pipeline/voyager_engine.py`). It connects to multiple RTSP streams, executes vehicle detection models, and filters bounding boxes based on size and aspect ratio limits. The `VehicleZoneAnalytics` module tracks vehicles across frames, executes point-in-polygon tests to determine zone occupancy, tracks individual dwell times, and emits aggregated state payloads and alerts via MQTT.

### 5.2 Central subsystems

A FastAPI server running asynchronously (`server/main.py`), utilizing asyncpg and aioredis.
1. **MQTT Consumer** — Listens to edge telemetry, parses incoming JSON payloads, updates Redis with the absolute latest state, and batches metrics for database insertion.
2. **Storage Layer** — TimescaleDB (PostgreSQL) handles high-throughput insertions of `vehicle_zone_metrics` and `vehicle_alerts`. Materialized views continuously aggregate metrics (1-minute and 1-hour rollups).
3. **WebSocket Gateway** — Listens to Redis Pub/Sub channels and selectively fans out real-time updates and alerts to connected dashboard clients based on their topic subscriptions.
4. **Alert Engine** — Persists critical events (overstays, overcrowding) to the database and optionally handles external routing.

### 5.3 Dashboard

A web-based interface (e.g., `server/static/dashboard.html`) that connects to the central server via WebSocket (`/ws/dashboard`). It subscribes to relevant zone topics and an `overview` topic to render live vehicle counts, occupancy gauges, and instant alert notifications.

## 6. Deployment topology (Fully On-Premise)

This topology leverages existing on-premise hardware for all system components. It ensures zero-latency, real-time compute, and complete data sovereignty since no data leaves the physical premises.

### 6.1 Stack

| Tier | Choice |
|---|---|
| MQTT broker (edge) | **Mosquitto** (running locally via Docker Compose) |
| Application Server | **Local On-Premise Server** running Docker Compose |
| Cache + internal bus | **Redis** (running locally via Docker Compose) |
| Dashboard hosting | **Nginx** (running locally via Docker Compose) |
| Database | **PostgreSQL 16** + Timescale extension (running locally, High Availability via Patroni) |
| Backups | **Automated Local Backups** to separate NAS/SAN |
| TLS / Auth | **Local Intranet Auth** (Active Directory / LDAP) or internal PKI |

### 6.2 Topology

```text
                                                Airport's Local Area Network (LAN)
                                                
   Operators                                                        
   (browsers) ─── HTTPS ──┐                                         
                          ▼                                         
         ┌───────────────────────────────────────────────────────────────┐
         │ On-Premise Application Server (Docker Compose)                │
         │   ├── Nginx (Reverse Proxy/UI)                                │
         │   ├── FastAPI Server ───────────────────────────────────────┐ │
         │   ├── Mosquitto (MQTT) ◀─────────────┐                      │ │
         │   ├── Redis                          │                      │ │
         │   └── PostgreSQL + TimescaleDB ◀─────┼──────────────────────┘ │
         └──────────────────────────────────────┼────────────────────────┘
                                                │
                                                │ MQTT (Port 1883/8883)
                                                │ Local LAN
                                                │
                                   ┌────────────┴───────────┐
                                   │ On-prem edge boxes     │
                                   └────────────────────────┘
```

### 6.3 Internal Connectivity

- **Edge to Application:** Edge boxes send telemetry directly to the local Mosquitto broker over the local airport LAN. The video data and high-frequency telemetry never traverse the public internet.
- **Application to Database:** The local FastAPI server connects directly to the local PostgreSQL instance over the Docker bridge network or local host interface. No cloud connection is established or required.

---

# Part II: Subsystem detail

## 7. Edge subsystem

### 7.1 Voyager inference engine

The `VoyagerEngine` leverages the Axelera Metis AIPU to decode and infer across multiple camera streams simultaneously using shared pipelines. Detections are extracted directly on the iteration thread and heavily filtered based on confidence, minimum/maximum bounding box area, and aspect ratios. The SDK handles Non-Maximum Suppression (NMS) natively. Raw frame pixels are copied into a SharedMemory `FrameSlot` for optional snapshot retrieval without pickling overhead.

### 7.2 Deep OC-SORT tracking

The tracker assigns stable `track_id`s to detections across frames. Each detection becomes the input to a Kalman filter. Deep OC-SORT reduces post-occlusion drift and limits ID swaps in dense scenes.

### 7.3 Vehicle zone analytics

The `VehicleZoneAnalytics` class processes tracked detections at 1 Hz. 
- **O(1) Zone Masking**: Configured zone polygons are converted into a 2D integer bitmask. The center point `(cx, cy)` of a vehicle's bounding box is used as an index against this mask to instantly determine if the vehicle is inside the zone.
- **Entry and Exit Tracking**: The system tracks vehicle IDs across frames. A vehicle appearing in the mask records an `entry_time`. Disappearing from the mask triggers an exit event.
- **Dwell Time**: Calculated dynamically as `current_time - entry_time`.
- **Smoothing**: To mitigate tracking flicker, the total vehicle count in a zone is passed through a 5-frame rolling median filter.

### 7.4 Edge alerting

Alerts are generated directly at the edge to ensure promptness.
- If a vehicle's dwell time exceeds the configured `max_dwell_time_s`, it flags an **overstay**.
- If the total number of vehicles exceeds the `max_vehicles` capacity, it flags an **overcrowding** state.
Alerts incorporate a cooldown mechanism (e.g., 900 seconds) to prevent spamming the broker while a violation persists.

### 7.5 Edge resilience and supervisor

The edge agent is a single Python process that can wedge or crash in ways the operator cannot tolerate (RTSP storms, Voyager SDK lock-ups, Metis NPU driver state). It is wrapped by a bash **supervisor** in `start.sh` that gives the stack three guarantees:

1. **Liveness.** A 90-second throughput-log watchdog detects the Voyager-SDK wedge state where the process is alive but produces no inference results. On stall the supervisor SIGKILLs the edge.
2. **Self-healing.** On any edge exit, the supervisor runs Metis cleanup (`pkill`, `fuser -k /dev/metis*`, `axdevice --refresh`) and relaunches. End-to-end recovery is ~30 seconds. The `axdevice --refresh` call requires four `sudo` operations (PCI remove/rescan, `modprobe -r metis`, `lsof /dev/metis*`); these are pre-authorized via a NOPASSWD sudoers rule (`deploy/axelera-metis-nopasswd.in`). Without that rule sudo blocks on a tty password prompt, which silently hangs the restart loop — observed in production as a 1h 36m outage on 2026-05-19.
3. **Loud failure.** After 5 fast crashes in a row (each under 120s of uptime) the supervisor gives up and publishes a retained MQTT message:

   ```
   vehicle/edge/{edge_id}/supervisor/state
     → {"state":"dead","reason":"rapid_crash_limit","crashes":5,"ts":...}
   ```

   The cloud alert engine subscribes (or should subscribe) to this topic and pages operators. Without it the stack would look "up" from the cloud's perspective — the FastAPI server and WebSocket gateway continue serving cached data — while the edge is permanently dead.

Defense in depth: `start.sh` wraps `axdevice --refresh` with `timeout 60 ... </dev/null` so that even if the sudoers rule is ever removed, the supervisor cannot block indefinitely; it simply skips the Metis reset and attempts the next launch.

## 8. Central subsystems

### 8.1 MQTT ingest and dispatch

The `MQTTConsumer` connects to the broker and subscribes to topics like `prefix/edge/+/zone/+` and `prefix/edge/+/alert/+`. Incoming messages are placed into an internal `queue.Queue`.
A dispatcher loop processes this queue:
- **Metrics**: Updates a Redis hash (`vzone:{zone_id}:latest`), publishes the raw JSON to the Redis Pub/Sub channel `vmetrics:{zone_id}`, and appends the data to an internal batch list.
- **Alerts**: Instantly inserts the alert into the `vehicle_alerts` PostgreSQL table and publishes to the Redis `valert:{zone_id}` channel.

A secondary flusher loop runs every 1 second to execute a bulk `INSERT` of all batched metrics into the TimescaleDB hypertable.

### 8.2 Storage layer (TimescaleDB)

The PostgreSQL 16 database relies on the TimescaleDB extension to manage high-volume time-series data.
- **`vehicle_zone_metrics`**: A hypertable partitioned by time (`ts`). Contains absolute vehicle counts, occupancy percentages, dwell times, and inference performance stats. It employs a 7-day compression policy.
- **Continuous Aggregates**: Materialized views (`vehicle_zone_metrics_1m` and `vehicle_zone_metrics_1h`) automatically aggregate maximums and averages over 1-minute and 1-hour intervals, drastically accelerating historical API queries.

### 8.3 WebSocket gateway

The FastAPI application mounts a WebSocket endpoint at `/ws/dashboard`. 
- Dashboard clients send subscription intents (e.g., `{"action": "subscribe", "topics": ["zone:zone-123", "overview"]}`).
- An internal asyncio task `_pubsub_listener()` listens to Redis pattern subscriptions (`vmetrics:*`, `valert:*`).
- Upon receiving a Redis message, the gateway loops through connected WebSocket clients and dispatches the payload only if the client is explicitly subscribed to that zone.
- An `_overview_timer()` executes every 1 second, aggregating the global state of all zones in memory, and broadcasting a unified `overview` packet to relevant subscribers.

### 8.4 API surface

REST endpoints providing configuration and historical data:

- `/api/zones`, `/api/cameras`, `/api/edges` — CRUD and read operations for system configuration.
- `/api/alerts` — Alert history and management.
- `/api/overview` — High-level statistical summaries.
- `/health` — System health check.
- `/dashboard` — Serves the static frontend dashboard.
- `/ws/dashboard` — Live WebSocket feed for metrics and alerts.

---

# Part III: Cross-cutting

## 9. Data design

### 9.1 Core tables

| Table | Purpose | Type |
|---|---|---|
| `edges` | Configuration and status of edge hardware devices | Standard Postgres |
| `cameras` | Configuration of RTSP sources assigned to edges | Standard Postgres |
| `zones` | Polygon definitions, max capacity, and dwell thresholds | Standard Postgres |
| `vehicle_zone_metrics` | 1 Hz telemetry emitted by edge agents | Timescale Hypertable |
| `vehicle_alerts` | Point-in-time overstay and overcrowding events | Standard Postgres |

### 9.2 MQTT topics

| Topic | Payload |
|---|---|
| `{prefix}/edge/{edge_id}/zone/{zone_id}` | JSON: `VehicleZoneMetricsMsg` (counts, dwell, occupancy) |
| `{prefix}/edge/{edge_id}/alert/{alert_type}` | JSON: `VehicleAlertMsg` (level, message, track_id) |
| `{prefix}/edge/{edge_id}/heartbeat` | JSON: `EdgeHeartbeatMsg` (CPU/GPU load, FPS, uptime) |

### 9.3 Database schema

Configuration tables:

```sql
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

CREATE TABLE zones (
    zone_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    camera_id       TEXT REFERENCES cameras(camera_id),
    zone_poly       JSONB,
    ramp_type       TEXT CHECK (ramp_type IN ('inner', 'outer')),
    max_vehicles    INT DEFAULT 20,
    max_dwell_time_s REAL DEFAULT 900.0,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

Time-series tables (TimescaleDB hypertables) and Alerts:

```sql
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
    acked_at        TIMESTAMPTZ
);
```

## 10. Runtime flows

### 10.1 Live metric update flow

```text
Edge Agent (Voyager + Analytics):
  - AI Pipeline detects vehicles in Frame N.
  - Tracker maintains track IDs.
  - Analytics executes point-in-polygon on bounding box centers.
  - Computes counts, max dwell, and occupancy.
  - Publishes `VehicleZoneMetricsMsg` to MQTT.

Central MQTT Consumer:
  - Receives MQTT payload.
  - Updates Redis Hash: `HSET vzone:{zone_id}:latest`.
  - Publishes to Redis: `PUBLISH vmetrics:{zone_id} {payload}`.
  - Appends to batch for 1-second DB flusher.

Central WebSocket Gateway:
  - Receives Redis pub/sub message.
  - Iterates connected WebSocket clients.
  - Pushes JSON frame to clients subscribed to that `zone_id`.

Dashboard:
  - Receives WebSocket frame.
  - Updates React state to render the new vehicle count in < 200ms.
```

### 10.2 Overstay alert flow

```text
Edge Agent:
  - Track ID 405 enters the zone. `entry_time` recorded.
  - 15 minutes pass. Dwell time exceeds `max_dwell_time_s` threshold.
  - Edge alert logic generates a `VehicleAlertMsg` with level="warning".
  - Records local cooldown to prevent duplicate alerts for 15 minutes.
  - Publishes alert to MQTT.

Central MQTT Consumer:
  - Immediately executes DB `INSERT` into `vehicle_alerts`.
  - Publishes to Redis: `PUBLISH valert:{zone_id} {payload}`.

Central WebSocket Gateway:
  - Forwards alert packet to subscribed dashboard clients.

Dashboard:
  - Renders a toast notification or adds row to the active Alerts table.
```

---

## 11. Sample Dashboard Output

### 11.1 Visual Screen Layout (Mockup)

The React Dashboard consumes the WebSocket feed to render live widgets. A typical operator view looks like this:

```text
================================================================================
  VEHICLE ZONE INTELLIGENCE Dashboard                    [ Live: Connected ● ]
================================================================================
  [OVERVIEW]
  Total Vehicles: 142     |     Active Zones: 8/8     |     Critical Alerts: 1
--------------------------------------------------------------------------------
  [ACTIVE ALERTS]
  ⚠️  ZONE: drop-off-A | Overstay Alert: Vehicle #405 dwelling 16m (Limit: 15m)
--------------------------------------------------------------------------------
  [ZONE DETAILS]

  Zone: Drop-off Lane A                  Zone: Parking Sector B
  ---------------------                  ----------------------
  Status:  [ OVERCROWDED ]               Status:  [ NORMAL ]
  Vehicles: 24 / 20 (120%)               Vehicles: 12 / 50 (24%)
  Avg Dwell: 4.2 min                     Avg Dwell: 14.5 min
  Max Dwell: 16.1 min                    Max Dwell: 45.2 min

  [ Breakdown ]                          [ Breakdown ]
  Cars: 18 | Trucks: 4 | Buses: 2        Cars: 12 | Trucks: 0 | Buses: 0
================================================================================
```

### 11.2 WebSocket Payload Samples

The UI above is driven entirely by the real-time JSON frames pushed by the WebSocket gateway:

**1. Live Metric Update (`zone_metric`)**
```json
{
  "type": "zone_metric",
  "zone_id": "drop-off-A",
  "data": {
    "edge_id": "vehicle-edge-01",
    "camera_id": "cam-front-1",
    "ts": 1714828345.12,
    "vehicle_count": 24,
    "vehicle_count_by_type": {"car": 18, "truck": 4, "bus": 2},
    "occupancy_pct": 120.0,
    "avg_dwell_time_s": 252.0,
    "max_dwell_time_s": 966.0,
    "overcrowding_alert": true,
    "overstay_count": 1
  }
}
```

**2. Alert Dispatch (`alert`)**
```json
{
  "type": "alert",
  "zone_id": "drop-off-A",
  "data": {
    "alert_type": "overstay",
    "level": "warning",
    "message": "Vehicle #405 dwelling for 966s (Limit: 900s)",
    "track_id": 405,
    "dwell_time_s": 966.0,
    "vehicle_count": 24,
    "ts": 1714828345.12
  }
}
```

## 12. Alternative Deployment Topologies

While the Fully On-Premise approach (Section 6) is the recommended and default architecture for maximum privacy and low latency, you may choose to run the central infrastructure completely in the cloud.

### 12.1 Fully Cloud-Based (Single EC2 Architecture)

For environments where centralized cloud management is preferred and minimizing cloud costs is a priority, this option moves the entire central infrastructure (API, database, message broker, dashboard) to a single AWS EC2 instance.

**Key Characteristics & Analysis:**
- **Consolidated Cloud Compute:** All central services are consolidated onto a single AWS EC2 instance (e.g., `t4g.large`).
- **Self-Managed Services in EC2:** The EC2 instance runs a comprehensive Docker Compose stack containing everything:
  - **Mosquitto MQTT Broker:** Ingests telemetry securely from edge devices over the internet via TLS.
  - **Redis:** Manages local Pub/Sub and fast state caching within the EC2 instance.
  - **FastAPI Backend & Nginx:** Serves the API, WebSockets, and hosts the static React dashboard directly from the instance.
  - **PostgreSQL + TimescaleDB:** The database is also run locally on the EC2 instance inside a container, utilizing an attached EBS volume for persistent storage, completely eliminating the need for a managed RDS instance.
- **Trade-offs:** 
  - **Data Durability Responsibility:** Since RDS is bypassed, automated EBS snapshots and robust backup scripts must be implemented and managed manually to ensure zero data loss.
  - **Internet Dependency:** If the local site internet goes down, edge telemetry cannot reach the cloud server, leading to temporary data gaps.
  - **Single Point of Failure:** If the single EC2 instance fails, the entire central dashboard and data ingest experience downtime until the instance recovers.
