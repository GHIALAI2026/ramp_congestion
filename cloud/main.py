"""
Vehicle Zone Intelligence — Cloud API server.

Run with: PYTHONPATH=. uvicorn cloud.main:app --host 127.0.0.1 --port 8002
(LAN access is via the Nginx reverse proxy on HTTPS/443, not directly on 8002.)

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
import re
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from cloud.config import settings
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

# CORS restricted to the approved dashboard origin(s) only — no wildcard
# (security observation #6). Configure via VZI_CORS_ALLOW_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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
_ALERT_IMG_DIR = (_STATIC_DIR / "alert_images").resolve()
# Alert evidence filenames are "{alert_id}.jpg" (see modules/alerts/image_capture).
_ALERT_IMG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.jpg$")


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard")
async def dashboard():
    # no-cache: the browser must revalidate the dashboard HTML on every load.
    # FileResponse still sends an ETag, so an unchanged page returns a cheap
    # 304 — but after a deploy (new JS, relabelled zones, etc.) operators
    # immediately get the fresh page instead of a heuristically-cached old
    # copy. Static assets that change (e.g. the ramp map image) are versioned
    # via a ?v= query string in dashboard.html.
    return FileResponse(
        _STATIC_DIR / "dashboard.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/static/alert_images/{filename}")
async def alert_image(filename: str):
    """Serve alert evidence images through a dedicated, validated handler
    instead of the blanket static mount (security observation #11).

    Access is gated by the Nginx perimeter — the app is bound to loopback and
    only reachable via Nginx with its source-IP allowlist — so direct
    unauthenticated LAN access is already blocked. This route adds
    path-traversal protection, disables any directory access, and sets a
    private cache policy. It is the single chokepoint where per-user
    authorization will attach once login lands (observation #1).
    """
    if not _ALERT_IMG_NAME_RE.match(filename):
        return JSONResponse(status_code=404, content={"error": "not_found"})
    path = (_ALERT_IMG_DIR / filename).resolve()
    # Defence in depth: ensure the resolved path stays inside the image dir.
    if not path.is_relative_to(_ALERT_IMG_DIR) or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


# Mount the rest of /static AFTER the alert-image route above, so that route
# takes precedence for /static/alert_images/* (StaticFiles never serves alert
# evidence directly). StaticFiles does not list directories (html=False), so
# directory listing is disabled (observation #11).
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


if __name__ == "__main__":
    # Bind to loopback only — the dashboard is exposed to the LAN through the
    # Nginx reverse proxy on HTTPS/443 (deploy/nginx/vehicle-dashboard.conf),
    # never directly on 8002.
    uvicorn.run(
        "cloud.main:app",
        host="127.0.0.1",
        port=8002,
        reload=False,
        log_level="info",
    )
