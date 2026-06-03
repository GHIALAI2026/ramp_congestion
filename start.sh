#!/usr/bin/env bash
# ==================================================================
# Vehicle Zone Intelligence — Full Stack Launcher
# ==================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SDK_ROOT="${SDK_ROOT:-/home/admin1/voyager-sdk}"
LOG_DIR="$ROOT/.logs"
mkdir -p "$LOG_DIR"

# Load deployment secrets (DB password, CORS origin, etc.) from deploy/.env if
# present, exporting them so both this script and the uvicorn child inherit
# them. deploy/.env is gitignored and must never be committed; see
# deploy/.env.example for the template.
if [[ -f "$ROOT/deploy/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/deploy/.env"
    set +a
fi

# Capture all stdout/stderr to disk while keeping it on the terminal. Without
# this the supervisor's restart/watchdog/give-up messages only existed on the
# controlling tty — the 1h 36m silent outage on 2026-05-19 was undiagnosable
# afterwards for exactly that reason.
exec > >(tee -a "$LOG_DIR/start.out") 2>&1

VOYAGER_PYTHON="${VOYAGER_PYTHON:-/home/admin1/voyager-sdk/venv/bin/python}"
VOYAGER_ACTIVATE="${VOYAGER_ACTIVATE:-/home/admin1/voyager-sdk/venv/bin/activate}"
VOYAGER_NETWORK="${VOYAGER_NETWORK:-yolov8s-coco}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
DASHBOARD_URL="http://localhost:8002/dashboard"
# LAN clients no longer reach :8002 directly (it's bound to loopback).
# They go through the Nginx reverse proxy on HTTPS/443.
if [[ -n "${LOCAL_IP:-}" ]]; then
    DASHBOARD_LAN_URL="https://$LOCAL_IP/dashboard  (via Nginx — requires deploy/nginx setup)"
else
    DASHBOARD_LAN_URL=""
fi

SUPERVISOR_SHUTDOWN=0

# Path to the root-owned Metis reset helper installed via the narrow sudoers
# rule (deploy/axelera-metis-nopasswd.in). Overridable for non-standard installs.
METIS_RESET_BIN="${METIS_RESET_BIN:-/usr/local/sbin/metis-reset.sh}"

metis_reset() {
    # Reset the Metis NPU through the root-owned helper (one narrow sudoers
    # rule), scoped strictly to the metis-bound PCI device. Falls back to
    # `axdevice --refresh` on a dev box where the helper isn't installed.
    # </dev/null ensures a sudo password prompt can never hang the restart loop.
    if [[ -x "$METIS_RESET_BIN" ]]; then
        timeout 60 sudo -n "$METIS_RESET_BIN" </dev/null 2>/dev/null || true
    else
        timeout 60 axdevice --refresh </dev/null 2>/dev/null || true
    fi
}

cleanup() {
    SUPERVISOR_SHUTDOWN=1
    info "Shutting down..."
    [[ -n "${SUPERVISOR_PID:-}" ]] && kill "$SUPERVISOR_PID" 2>/dev/null || true
    [[ -n "${CLOUD_PID:-}" ]] && kill "$CLOUD_PID" 2>/dev/null || true
    [[ -n "${EDGE_PID:-}"  ]] && kill "$EDGE_PID"  2>/dev/null || true
    # Kill any lingering edge_agent.main children spawned by the supervisor.
    pkill -f "edge_agent.main" 2>/dev/null || true
    wait 2>/dev/null

    info "Hunting down and killing zombie processes on the Metis NPU..."
    fuser -k -9 /dev/metis* 2>/dev/null || true
    sleep 1

    info "Resetting Metis NPU (root-owned helper)..."
    metis_reset

    info "Done."
}
trap cleanup EXIT

# ------------------------------------------------------------------
# 1. Check infrastructure
# ------------------------------------------------------------------
info "Checking infrastructure..."

for svc in mosquitto postgresql redis-server; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "  ✓ $svc running"
    else
        warn "  ✗ $svc not running — attempting start..."
        sudo -n systemctl start "$svc" 2>/dev/null \
            || error "  Failed to start $svc (sudo needs password, or unit failed) — start it yourself: sudo systemctl start $svc"
    fi
done

# ------------------------------------------------------------------
# 2. Initialize database (if needed)
# ------------------------------------------------------------------
DB_NAME="${VZI_DB_NAME:-vehicle_zone}"
DB_USER="${VZI_DB_USER:-vzi_app}"
DB_HOST="${VZI_DB_HOST:-localhost}"
DB_PORT="${VZI_DB_PORT:-5432}"

# No default password: production must not retain default DB credentials
# (security observation #5). The password comes from the environment, normally
# via deploy/.env which is sourced at the top of this script.
if [[ -z "${VZI_DB_PASSWORD:-}" ]]; then
    error "VZI_DB_PASSWORD is not set."
    error "  Copy deploy/.env.example to deploy/.env and set a strong DB password."
    error "  Default credentials are not permitted."
    exit 1
fi
export PGPASSWORD="$VZI_DB_PASSWORD"

if psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -lqt 2>/dev/null | grep -qw "$DB_NAME"; then
    info "Database '$DB_NAME' already exists"
else
    info "Creating database '$DB_NAME'..."
    createdb -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" "$DB_NAME" 2>/dev/null || true
    psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" 2>/dev/null || true
    psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -f "$ROOT/schema.sql"
    info "Database initialized"
fi

unset PGPASSWORD

# ------------------------------------------------------------------
# 3. Start cloud server
# ------------------------------------------------------------------
# Kill any stale process on port 8002 from a previous run
lsof -ti:8002 | xargs kill -9 2>/dev/null || true
sleep 0.5

info "Starting cloud server on 127.0.0.1:8002..."
cd "$ROOT"
# Bind to loopback only. The dashboard must be reached through the Nginx
# reverse proxy on HTTPS/443 (see deploy/nginx/vehicle-dashboard.conf), not
# directly on 8002 from the LAN. The server console can still use
# http://localhost:8002 locally.
PYTHONPATH="$ROOT:$SDK_ROOT" python3 -m uvicorn cloud.main:app \
    --host 127.0.0.1 --port 8002 --log-level info \
    > "$LOG_DIR/cloud.log" 2>&1 &
CLOUD_PID=$!
sleep 2

if kill -0 "$CLOUD_PID" 2>/dev/null; then
    info "  ✓ Cloud server running (PID $CLOUD_PID)"
else
    error "  ✗ Cloud server failed to start — check $LOG_DIR/cloud.log"
    exit 1
fi

# ------------------------------------------------------------------
# 4. Start edge agent
# ------------------------------------------------------------------
info "Starting edge agent (Voyager SDK)..."
cd "$ROOT"
if [[ ! -x "$VOYAGER_PYTHON" ]]; then
    error "  ✗ Voyager Python not found at $VOYAGER_PYTHON"
    error "    Set VOYAGER_PYTHON to the SDK venv python before running start.sh"
    exit 1
fi
if [[ ! -f "$VOYAGER_ACTIVATE" ]]; then
    error "  ✗ Voyager activate script not found at $VOYAGER_ACTIVATE"
    error "    Set VOYAGER_ACTIVATE to the SDK venv activate script before running start.sh"
    exit 1
fi

info "  Using Voyager network: $VOYAGER_NETWORK"
source "$VOYAGER_ACTIVATE"
export NUM_ANALYTICS_SHARDS=3

# Single-thread the math libraries. Each analytics shard is pinned to one
# core; the bundled OpenBLAS (numpy + opencv-python) otherwise spawns ~16
# worker threads per process that pile onto that one core and thrash the
# GIL — observed as ~180M nonvoluntary context switches per shard. These
# must be exported before Python imports numpy/cv2.
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export BLIS_NUM_THREADS=1

# Supervisor: restart the edge if it exits OR if it hangs (no throughput
# updates for HANG_WATCHDOG_S). Necessary because the Voyager SDK can wedge
# itself ("No pipeline configs provided" cascade) and the edge's own
# os._exit(2) can hang in atexit/log-flush before actually exiting.
HANG_WATCHDOG_S=90       # kill if throughput log hasn't grown for this long
STARTUP_GRACE_S=45       # don't enforce watchdog right after a (re)start
MIN_HEALTHY_RUN_S=120    # under this counts as a "fast crash" for backoff
RAPID_CRASH_LIMIT=5      # give up after N fast crashes in a row

supervise_edge() {
    # Disable errexit inside the supervisor: `wait` returns the child's exit
    # code (137 on SIGKILL, non-zero on any crash), which is the WHOLE point
    # of the supervisor — these aren't failures to bail on, they're the
    # signal to restart.
    set +e
    local rapid_crashes=0
    while [[ $SUPERVISOR_SHUTDOWN -eq 0 ]]; do
        info "  → launching edge agent attempt..."
        PYTHONPATH="$ROOT:$SDK_ROOT" AXELERA_FRAMEWORK="$SDK_ROOT" VOYAGER_NETWORK="$VOYAGER_NETWORK" \
            "$VOYAGER_PYTHON" -m edge_agent.main >> "$LOG_DIR/edge.log" 2>&1 &
        local pid=$!
        local started_at=$(date +%s)
        info "  → edge pid=$pid"

        # Watchdog loop: poll until the edge exits OR throughput stalls.
        local last_size=0
        while kill -0 "$pid" 2>/dev/null; do
            sleep 15
            local age=$(( $(date +%s) - started_at ))
            if [[ $age -gt $STARTUP_GRACE_S ]]; then
                local cur_size=$(stat -c%s "$LOG_DIR/throughput.log" 2>/dev/null || echo 0)
                if [[ $cur_size -le $last_size ]]; then
                    # Throughput log isn't growing — give it $HANG_WATCHDOG_S total of silence
                    # then SIGKILL.
                    local silence_for=15
                    while kill -0 "$pid" 2>/dev/null && [[ $silence_for -lt $HANG_WATCHDOG_S ]]; do
                        sleep 15
                        silence_for=$((silence_for + 15))
                        local new_size=$(stat -c%s "$LOG_DIR/throughput.log" 2>/dev/null || echo 0)
                        if [[ $new_size -gt $last_size ]]; then
                            cur_size=$new_size
                            break
                        fi
                    done
                    if kill -0 "$pid" 2>/dev/null && [[ $(stat -c%s "$LOG_DIR/throughput.log" 2>/dev/null || echo 0) -le $last_size ]]; then
                        warn "  → edge pid=$pid silent for ${HANG_WATCHDOG_S}s — SIGKILL"
                        kill -9 "$pid" 2>/dev/null || true
                        # Also reap any worker children that survived
                        pkill -9 -P "$pid" 2>/dev/null || true
                        break
                    fi
                fi
                last_size=$cur_size
            fi
        done

        # Bounded wait. Plain `wait "$pid"` blocks forever on a half-dead
        # process — observed in production: edge initiated graceful
        # shutdown, GStreamer cleanup hung in PAUSED state, ~116 leaked
        # shared_memory objects, the python main exited but worker
        # subprocesses kept the pid technically alive. `wait` never
        # returned and the supervisor sat for 2+ hours doing nothing.
        # Hard-cap the wait at SHUTDOWN_TIMEOUT_S, then SIGKILL anything
        # still around so the restart loop can actually fire.
        local SHUTDOWN_TIMEOUT_S=30
        local shutdown_waited=0
        while kill -0 "$pid" 2>/dev/null && [[ $shutdown_waited -lt $SHUTDOWN_TIMEOUT_S ]]; do
            sleep 1
            shutdown_waited=$((shutdown_waited + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "  → edge pid=$pid still alive ${SHUTDOWN_TIMEOUT_S}s after watchdog exit; SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
            pkill -9 -P "$pid" 2>/dev/null || true
            sleep 1
        fi
        wait "$pid" 2>/dev/null
        local exit_code=$?
        local ran_for=$(( $(date +%s) - started_at ))
        warn "  → edge pid=$pid exited (code=$exit_code) after ${ran_for}s"

        [[ $SUPERVISOR_SHUTDOWN -eq 1 ]] && break

        # Pre-restart cleanup so the Metis device starts clean.
        pkill -9 -f "edge_agent.main" 2>/dev/null || true
        fuser -k -9 /dev/metis* 2>/dev/null || true
        sleep 1
        metis_reset

        # Backoff: if the edge crashed fast, slow down and bail after N in a row.
        if [[ $ran_for -lt $MIN_HEALTHY_RUN_S ]]; then
            rapid_crashes=$((rapid_crashes + 1))
            if [[ $rapid_crashes -ge $RAPID_CRASH_LIMIT ]]; then
                error "  → edge crashed $RAPID_CRASH_LIMIT times in a row (<${MIN_HEALTHY_RUN_S}s each); giving up"
                # Publish a retained 'dead' state so the cloud alert engine can
                # notice the supervisor has stopped trying. Until this was added
                # the stack looked 'up' (PIDs alive, cached data served via
                # WebSocket) while the edge was permanently down. Silent if
                # mosquitto-clients isn't installed.
                if command -v mosquitto_pub >/dev/null 2>&1; then
                    mosquitto_pub -h "${MQTT_HOST:-localhost}" -p "${MQTT_PORT:-1883}" \
                        -t "vehicle/edge/${EDGE_ID:-vehicle-edge-01}/supervisor/state" \
                        -r -q 1 -W 5 \
                        -m "{\"state\":\"dead\",\"reason\":\"rapid_crash_limit\",\"crashes\":$rapid_crashes,\"ts\":$(date +%s)}" \
                        2>/dev/null || true
                fi
                break
            fi
            warn "  → fast crash #$rapid_crashes — sleeping 10s before retry"
            sleep 10
        else
            rapid_crashes=0
            warn "  → restarting edge in 3s..."
            sleep 3
        fi
    done
    info "  → supervisor exiting"
}

supervise_edge &
SUPERVISOR_PID=$!
sleep 3

if kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    info "  ✓ Edge supervisor running (PID $SUPERVISOR_PID, watchdog=${HANG_WATCHDOG_S}s)"
else
    error "  ✗ Edge supervisor failed to start — check $LOG_DIR/edge.log"
fi

# ------------------------------------------------------------------
# 5. Summary
# ------------------------------------------------------------------
echo ""
info "=== Vehicle Zone Intelligence Running ==="
info "  Dashboard:      $DASHBOARD_URL"
if [[ -n "$DASHBOARD_LAN_URL" ]]; then
    info "  Dashboard LAN:  $DASHBOARD_LAN_URL"
fi
info "  Cloud API:      http://localhost:8002"
info "  Cloud Health:   http://localhost:8002/health"
info "  Edge Snapshots: http://localhost:8003/stream/{camera_id}"
info "  MQTT Broker:    localhost:1883"
info "  Logs:           $LOG_DIR/"
echo ""
info "Press Ctrl+C to stop all services"

wait
