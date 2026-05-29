"""
Vehicle Zone Intelligence — Cloud API server.

Run with: PYTHONPATH=. uvicorn cloud.main:app --host 0.0.0.0 --port 8002

Startup:
  1. Connect PostgreSQL (asyncpg)
  2. Connect Redis
  3. Start MQTT consumer
  4. Start Edge publisher
  5. Start Alert engine
  6. Start WebSocket gateway tasks
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from cloud.models.db import init_db, close_db, init_redis, close_redis
from cloud.modules.ingestion.mqtt_consumer import MQTTConsumer
from cloud.modules.alerts.alert_engine import AlertEngine
from cloud.modules.maintenance.camera_watchdog import CameraWatchdog
from cloud.modules.websocket.gateway import setup_websocket, start_ws_tasks, stop_ws_tasks
from cloud.comms.edge_publisher import EdgePublisher
from cloud.modules.auth import AdminOnlyMiddleware, is_admin_request

from cloud.modules.api.zones import router as zones_router
from cloud.modules.api.zone_groups import router as zone_groups_router
from cloud.modules.api.cameras import router as cameras_router
from cloud.modules.api.alerts import router as alerts_router
from cloud.modules.api.overview import router as overview_router
from cloud.modules.api.analytics import router as analytics_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vehicle-cloud")

_mqtt_consumer: MQTTConsumer | None = None
_edge_publisher: EdgePublisher | None = None
_alert_engine: AlertEngine | None = None
_camera_watchdog: CameraWatchdog | None = None


async def _ensure_schema_compat(db) -> None:
    """Apply lightweight backwards-compatible schema updates for existing DBs."""
    await db.execute(
        """
        ALTER TABLE zones
        ADD COLUMN IF NOT EXISTS ramp_type TEXT
        CHECK (ramp_type IN ('inner', 'outer'))
        """
    )
    # Zone groups: one logical zone covered by N cameras. Each camera-zone
    # row keeps its own polygon and its own per-camera count, but the UI
    # aggregates the group's total (sum across member cameras) for the
    # "Zone 5 has 7 vehicles across 4 feeds" operator view. zone_group_id
    # on zones is NULL for the existing single-camera-zone case, so this
    # migration is fully backward-compatible.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS zone_groups (
            group_id          TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            max_vehicles      INT,
            max_dwell_time_s  REAL,
            created_at        TIMESTAMPTZ DEFAULT NOW(),
            updated_at        TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    await db.execute(
        """
        ALTER TABLE zones
        ADD COLUMN IF NOT EXISTS zone_group_id TEXT
        REFERENCES zone_groups(group_id) ON DELETE SET NULL
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_zones_group ON zones (zone_group_id) "
        "WHERE zone_group_id IS NOT NULL"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mqtt_consumer, _edge_publisher, _alert_engine, _camera_watchdog
    logger.info("=== Vehicle Zone Intelligence Cloud Server starting ===")

    # 1. Database
    db = await init_db()
    await _ensure_schema_compat(db)
    logger.info("✓ PostgreSQL connected")

    # 2. Redis
    redis = await init_redis()
    logger.info("✓ Redis connected")

    # 3. MQTT Consumer
    _mqtt_consumer = MQTTConsumer()
    await _mqtt_consumer.start()
    logger.info("✓ MQTT consumer started")

    # 4. Edge Publisher
    _edge_publisher = EdgePublisher()
    await _edge_publisher.start()
    app.state.edge_publisher = _edge_publisher
    logger.info("✓ Edge publisher started")

    # 5. Alert Engine
    _alert_engine = AlertEngine()
    await _alert_engine.start(db, redis)
    logger.info("✓ Alert engine started")

    # 6. WebSocket tasks
    await start_ws_tasks()
    logger.info("✓ WebSocket gateway started")

    # 7. Camera watchdog (ONVIF auto-reboot for hung RTSP daemons)
    _camera_watchdog = CameraWatchdog()
    await _camera_watchdog.start(db, redis)
    logger.info("✓ Camera watchdog started")
    app.state.camera_watchdog = _camera_watchdog

    logger.info("Cloud server ready on port 8002")

    yield

    logger.info("Cloud server shutting down...")
    if _camera_watchdog:
        await _camera_watchdog.stop()
    await stop_ws_tasks()
    if _alert_engine:
        await _alert_engine.stop()
    if _edge_publisher:
        await _edge_publisher.stop()
    if _mqtt_consumer:
        await _mqtt_consumer.stop()
    await close_redis()
    await close_db()
    logger.info("Cloud server stopped")


app = FastAPI(
    title="Vehicle Zone Intelligence API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AdminOnlyMiddleware)

# API routers
app.include_router(zones_router)
app.include_router(zone_groups_router)
app.include_router(cameras_router)
app.include_router(alerts_router)
app.include_router(overview_router)
app.include_router(analytics_router)

# WebSocket
setup_websocket(app)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "vehicle-zone-intelligence"}


@app.get("/api/whoami")
async def whoami(request: Request):
    """Tell the dashboard whether the requester is admin (server console)
    or a remote viewer, so it can render the appropriate UI."""
    return {"is_admin": is_admin_request(request)}


_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard")
async def dashboard():
    return FileResponse(_STATIC_DIR / "dashboard.html")


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


if __name__ == "__main__":
    uvicorn.run(
        "cloud.main:app",
        host="0.0.0.0",
        port=8002,
        reload=False,
        log_level="info",
    )
