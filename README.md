# Vehicle Zone Intelligence

Real-time vehicle counting and zone analytics using Axelera Voyager SDK (Metis AIPU) for edge inference.

## Features

- **Vehicle detection**: YOLO on Axelera Metis AIPU (car, motorcycle, bus, truck)
- **OC-SORT tracking**: Persistent track IDs across frames
- **Zone analytics**: Per-zone vehicle count, occupancy %, dwell times
- **Overcrowding alerts**: Fires when vehicle count exceeds zone threshold
- **Overstay alerts**: Fires when individual vehicle exceeds dwell time limit
- **Full cloud pipeline**: MQTT → Redis + TimescaleDB → WebSocket → Dashboard

## Architecture

```
RTSP Cameras → GStreamer (VA-API decode on Intel iGPU)
  → Axelera Metis AIPU (yolov8s-coco vehicle detection)
    → OC-SORT Tracking
      → VehicleZoneAnalytics (per-zone metrics at 1 Hz)
        → MQTT (Mosquitto)
          → Cloud (FastAPI + Redis + TimescaleDB)
            → WebSocket Gateway
              → Dashboard
```

## Prerequisites

System packages (the edge host needs all of these; the cloud host needs
mosquitto, postgresql-16 + timescaledb, redis-server):

```bash
sudo apt install mosquitto mosquitto-clients postgresql-16 redis-server \
                 psmisc lsof intel-gpu-tools ffmpeg
# Plus TimescaleDB from the official APT repo (https://docs.timescale.com)
# Plus the Axelera Voyager SDK installed at /home/$USER/voyager-sdk (or set
# SDK_ROOT / VOYAGER_PYTHON / VOYAGER_ACTIVATE before running start.sh)
```

`mosquitto-clients` is required for the supervisor's "dead" MQTT publish on
giveup. `psmisc` provides `fuser`, used during Metis NPU cleanup.

## Quick Start

> **Important:** step 1 must come before step 2. The edge agent must be
> installed into the Voyager SDK's venv (`pip install` while the SDK venv is
> active). Installing into a different venv silently produces a
> non-functional edge — `axdevice` and the Voyager Python bindings will be
> missing.

```bash
# 1. Activate Axelera Voyager SDK (REQUIRED before step 2)
source /path/to/voyager-sdk/venv/bin/activate

# 2. Install dependencies into the active SDK venv
pip install -r requirements-edge.txt
pip install -r requirements-cloud.txt

# 3. Start everything
chmod +x start.sh
./start.sh
```

Development happens on the `dev` branch and is merged to `main` for
production deploys. Open PRs against `main`.

## One-time host setup (per machine)

`start.sh` runs a supervisor that restarts the edge agent on crashes. As part
of restart cleanup it invokes `axdevice --refresh`, which internally runs four
`sudo` commands to reset the Metis NPU (PCI remove/rescan, unload the kernel
module, lsof the device files). Without a NOPASSWD rule those `sudo` calls
block on a tty password prompt and the supervisor's restart loop silently
hangs — we observed a 1h 36m outage caused by this.

Install the bundled sudoers rule once per machine (requires your password
exactly once, since writing to `/etc/sudoers.d/` is itself a root operation):

```bash
sed "s/__USER__/$USER/" deploy/axelera-metis-nopasswd.in \
  | sudo install -o root -g root -m 0440 /dev/stdin \
      /etc/sudoers.d/axelera-metis-nopasswd

# Verify (both should print exit=0):
sudo -n /usr/bin/tee /sys/bus/pci/rescan </dev/null >/dev/null ; echo "rescan exit=$?"
sudo -n /usr/bin/lsof /dev/metis* >/dev/null 2>&1 ; echo "lsof exit=$?"
```

The rule is scoped to four exact commands needed for Metis recovery; it does
not broaden any other sudo capability. The target user must already be in the
`sudo` group.

## Resilience and operations

### Supervisor behaviour

`start.sh` launches three things: the cloud server (uvicorn on :8002), the
edge agent, and a bash **supervisor** that watches the edge.

- **Hang watchdog** — if the throughput log stops growing for `90s`
  (`HANG_WATCHDOG_S`), the supervisor SIGKILLs the edge. Voyager can wedge
  in a way that keeps the process alive but stops producing inference
  results; this is the only way to detect it.
- **Crash restart** — when the edge exits for any reason (crash, the
  `voyager_engine.py:604` self-SIGKILL, watchdog, …) the supervisor cleans
  up Metis state (`axdevice --refresh`) and relaunches after a 3-second
  backoff. Healthy → unhealthy → healthy round-trip is ~30 seconds.
- **Rapid-crash backoff** — if the edge dies under `120s`
  (`MIN_HEALTHY_RUN_S`), it's counted as a fast crash and backoff goes to
  10s. After `5` consecutive fast crashes (`RAPID_CRASH_LIMIT`) the
  supervisor **gives up** and publishes a retained MQTT message:

    ```
    Topic:   vehicle/edge/{edge_id}/supervisor/state
    Payload: {"state":"dead","reason":"rapid_crash_limit","crashes":5,"ts":...}
    ```

  Subscribe to that topic from any alerting layer to page on supervisor
  giveup — the giveup state otherwise looks "healthy" from the outside
  (cloud server keeps responding, dashboard serves cached WebSocket data).

### Log files

All under `.logs/`:

| File | What's in it | Look at it when |
|---|---|---|
| `start.out` | Supervisor restart events, watchdog kills, giveup messages, infrastructure check output | Diagnosing why the edge isn't running |
| `edge.log` | Edge agent stdout/stderr (cameras, Voyager, analytics, MQTT publisher) | Edge restarts, RTSP errors, Voyager wedges |
| `cloud.log` | Cloud server (uvicorn) — API requests, MQTT consumer, alert engine | API errors, alerts not firing, WebSocket issues |
| `throughput.log` | One row per inference batch; used by the supervisor watchdog | Verifying inference is actually flowing; FPS regressions |

### Runbook — what to do when

**Edge keeps restarting (visible in `edge.log` as repeated `=== Vehicle Zone
Intelligence Edge Agent starting ===` lines)** — grep `edge.log` for the
last `CRITICAL` or `Traceback`. Common causes: Voyager SDK wedged (search
`No pipeline configs provided`), Metis device contention (`axr_device_connect
failed`), or RTSP camera offline (`Could not read from resource`).

**Voyager wedged with "No pipeline configs"** — this is the failure mode that
caused the 2026-05-19 outage. The edge self-SIGKILLs, supervisor restarts it
within ~30s. No action needed unless it keeps recurring (then suspect a
specific camera or a recent SDK update). If `axdevice --refresh` is the
thing that's hanging, see "Metis NPU stuck" below.

**Metis NPU stuck (next launch aborts with `axr_device_connect failed`)** —
manual recovery:

```bash
sudo modprobe -r metis
echo 1 | sudo tee /sys/bus/pci/rescan
sudo modprobe metis  # if needed; usually rescan picks it up
```

If you've never installed the sudoers rule from the section above, the
supervisor's automatic `axdevice --refresh` will silently hang waiting for
a password — install the rule.

**Supervisor gave up** — grep `.logs/start.out` for `crashed .* times in a
row`. The supervisor has stopped trying; `start.sh` is still alive but the
edge is permanently down. Inspect the crash cause in `edge.log`, fix the
underlying issue, and rerun `./start.sh`.

**Cloud says edge offline but edge looks healthy locally** — check
`heartbeat_loop` is still running (`pgrep -fa edge_agent.main` should show
the parent process and ~5 children). Check the MQTT broker is reachable
(`mosquitto_sub -h localhost -t '#' -v` for 5 seconds). Check `cloud.log`
for ingest errors.

## Configuration

### Environment Variables (Edge)

| Variable | Default | Description |
|----------|---------|-------------|
| `EDGE_ID` | `vehicle-edge-01` | Unique edge device ID |
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `CLOUD_API_URL` | `http://localhost:8002` | Cloud API base URL |
| `TARGET_FPS` | `8` | Inference frames per second |
| `CALLBACK_QUEUE_SIZE` | `1` | Per-camera edge callback backlog; `1` keeps latest frame only |
| `SNAPSHOT_CACHE_FPS` | `2` | Raw snapshot cache refresh rate |
| `LIVE_ANNOTATE_FPS` | `2` | Annotated preview render/stream rate |
| `INF_CONF` | `0.25` | Detection confidence threshold |
| `VOYAGER_NETWORK` | `yolov8s-coco` | Voyager SDK detection model |
| `OC_MAX_TIME_LOST` | `120` | OC-SORT max lost frames (15s) |
| `MQTT_TELEMETRY_QOS` | `0` | QoS for high-volume metrics/heartbeats |

### Environment Variables (Cloud)

| Variable | Default | Description |
|----------|---------|-------------|
| `VZI_DATABASE_URL_RAW` | `postgresql://...` | TimescaleDB URL |
| `VZI_REDIS_URL` | `redis://localhost:6379/1` | Redis URL (DB 1) |
| `VZI_MQTT_HOST` | `localhost` | MQTT broker host |
| `VZI_PROBE_CAMERA_SNAPSHOTS` | `false` | Enable expensive snapshot probes in `/api/cameras` |
| `VZI_MQTT_INGEST_QUEUE_MAX` | `1000` | Max cloud MQTT messages queued before stale drops |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET/POST | `/api/zones` | List/create zones |
| GET | `/api/zones/{id}/live` | Live metrics from Redis |
| GET | `/api/zones/{id}/history` | Time-series from TimescaleDB |
| GET/POST | `/api/cameras` | List/create cameras |
| GET | `/api/alerts` | Alert history |
| POST | `/api/alerts/{id}/acknowledge` | Acknowledge alert |
| GET | `/api/overview` | Aggregated dashboard data |
| WS | `/ws/dashboard` | Real-time metric stream |

## Ports

| Service | Port |
|---------|------|
| Cloud API | 8002 |
| Edge HTTP | 8003 |
| MQTT Broker | 1883 |
| PostgreSQL (TimescaleDB) | 5432 |
| Redis | 6379 |
