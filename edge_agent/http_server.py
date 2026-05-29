"""
Lightweight HTTP server for camera snapshots.
Port 8003 — proxied by the cloud server's snapshot endpoint.

GET /snapshot/{camera_id}  → raw JPEG frame
GET /annotated/{camera_id} → JPEG with overlays (from SharedMemory slot)
GET /stream/{camera_id}    → MJPEG stream (annotated)
GET /health                → {"status": "ok"}
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from edge_agent import config as cfg

logger = logging.getLogger(__name__)


async def _snapshot(request: web.Request) -> web.Response:
    """Raw (un-annotated) JPEG. Used by overstay alert evidence capture
    so the cropped vehicle has no bbox/label overlay clutter.

    Registers a viewer briefly so the Voyager iter thread starts memcpying
    raw frames into the FrameSlot (it gates writes on viewer presence to
    save CPU). Polls up to ~1s for a frame to appear before giving up.
    Without the viewer registration, cold /snapshot calls would return
    503 and the caller would fall back to /annotated — defeating the
    whole point of having a raw endpoint.
    """
    camera_id = request.match_info["camera_id"]
    camera_manager = request.app["camera_manager"]

    registered = camera_manager.add_viewer(camera_id)
    try:
        jpeg = None
        for _ in range(50):
            jpeg = camera_manager.get_snapshot(camera_id)
            if jpeg is not None:
                break
            await asyncio.sleep(0.02)
        if jpeg is None:
            raise web.HTTPServiceUnavailable(reason=f"No snapshot for {camera_id}")
        return web.Response(body=jpeg, content_type="image/jpeg")
    finally:
        if registered:
            camera_manager.remove_viewer(camera_id)


async def _annotated(request: web.Request) -> web.Response:
    """Single annotated snapshot."""
    camera_id = request.match_info["camera_id"]
    camera_manager = request.app["camera_manager"]

    registered = camera_manager.add_viewer(camera_id)
    try:
        jpeg = None
        for _ in range(50):
            jpeg = camera_manager.get_annotated_snapshot(camera_id)
            if jpeg is not None:
                break
            await asyncio.sleep(0.02)
        if jpeg is None:
            jpeg = camera_manager.get_snapshot(camera_id)
        if jpeg is None:
            raise web.HTTPServiceUnavailable(reason=f"No frame for {camera_id}")
        return web.Response(body=jpeg, content_type="image/jpeg")
    finally:
        if registered:
            camera_manager.remove_viewer(camera_id)


async def _mjpeg_stream(request: web.Request) -> web.StreamResponse:
    """MJPEG stream — push annotated frames as they arrive."""
    camera_id = request.match_info["camera_id"]
    camera_manager = request.app["camera_manager"]

    if not camera_manager.add_viewer(camera_id):
        raise web.HTTPNotFound(reason=f"Unknown camera {camera_id}")

    resp = web.StreamResponse()
    resp.content_type = "multipart/x-mixed-replace; boundary=frame"
    await resp.prepare(request)

    last_ts = 0.0
    frame_delay_s = 1.0 / max(0.1, float(getattr(cfg, "LIVE_ANNOTATE_FPS", 2.0)))
    try:
        while True:
            entry = camera_manager.get_annotated_snapshot_with_ts(camera_id)
            if entry is not None:
                jpeg, ts = entry
                if ts > last_ts:
                    last_ts = ts
                    await resp.write(
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                        + jpeg + b"\r\n"
                    )
            else:
                jpeg = camera_manager.get_snapshot(camera_id)
                if jpeg:
                    await resp.write(
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                        + jpeg + b"\r\n"
                    )
            await asyncio.sleep(frame_delay_s)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        camera_manager.remove_viewer(camera_id)
    return resp


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _camera_stats(request: web.Request) -> web.Response:
    """Per-camera resolution + fps for the dashboard cameras page."""
    camera_manager = request.app["camera_manager"]
    return web.json_response(camera_manager.get_camera_stats())


def create_app(camera_manager) -> web.Application:
    app = web.Application()
    app["camera_manager"] = camera_manager
    app.router.add_get("/snapshot/{camera_id}", _snapshot)
    app.router.add_get("/annotated/{camera_id}", _annotated)
    app.router.add_get("/stream/{camera_id}", _mjpeg_stream)
    app.router.add_get("/camera_stats", _camera_stats)
    app.router.add_get("/health", _health)
    return app


async def run_http_server(camera_manager) -> None:
    app = create_app(camera_manager)
    access_log = logger if cfg.HTTP_ACCESS_LOG else None
    runner = web.AppRunner(app, access_log=access_log)
    await runner.setup()
    # Bind to loopback only — the cloud server lives on the same host and
    # reaches us via http://localhost:8003. Exposing raw camera streams on
    # the LAN would bypass the cloud's admin/viewer gating in cloud/modules/auth.py.
    site = web.TCPSite(runner, "127.0.0.1", cfg.HTTP_PORT)
    await site.start()
    logger.info("Edge HTTP server running on 127.0.0.1:%d", cfg.HTTP_PORT)
    await asyncio.Event().wait()
