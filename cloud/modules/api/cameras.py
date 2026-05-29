"""Cameras API: CRUD, edge sync, and live view proxying."""

from __future__ import annotations

import asyncio
import time
import httpx
from fastapi import APIRouter, HTTPException, Body, Request
from fastapi.responses import Response, StreamingResponse

from cloud.config import settings
from cloud.models.db import get_db, get_redis
from rtsp_utils import normalize_rtsp_url

router = APIRouter(prefix="/api", tags=["cameras"])

_edge_client = httpx.AsyncClient(timeout=5.0)


async def _proxy_edge(path: str, detail: str) -> Response:
    try:
        resp = await _edge_client.get(f"{settings.edge_http_base_url}{path}")
    except httpx.ConnectError:
        raise HTTPException(503, "Edge HTTP server is not running")
    except httpx.TimeoutException:
        raise HTTPException(503, "Edge HTTP server timed out")

    if resp.status_code != 200:
        raise HTTPException(503, detail)

    content_type = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=resp.content, media_type=content_type)


async def _edge_heartbeat(edge_id: str) -> dict[str, str]:
    redis = await get_redis()
    return await redis.hgetall(f"vedge:{edge_id}:heartbeat")


async def _camera_stream_available(camera_id: str) -> bool:
    try:
        resp = await _edge_client.get(
            f"{settings.edge_http_base_url}/snapshot/{camera_id}",
            timeout=1.5,
        )
        return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
        return False


async def _live_camera_status(camera: dict) -> str:
    edge_id = camera.get("assigned_edge")
    if not edge_id:
        return camera.get("status") or "unknown"

    hb = await _edge_heartbeat(edge_id)
    if not hb:
        return "unknown"

    hb_ts = float(hb.get("ts", 0) or 0)
    if hb_ts and time.time() - hb_ts > 15.0:
        return "offline"

    # Snapshot probing is expensive with many cameras because it forces the
    # edge to JPEG-encode every source. Keep it opt-in for diagnostics.
    if settings.probe_camera_snapshots and await _camera_stream_available(camera["camera_id"]):
        return "online"

    error_cameras_raw = hb.get("error_cameras", "[]")
    try:
        import json

        error_cameras = set(json.loads(error_cameras_raw))
    except Exception:
        error_cameras = set()

    if camera["camera_id"] in error_cameras:
        return "error"

    if int(float(hb.get("cameras_assigned", 0))) > 0:
        return "online"

    return camera.get("status") or "unknown"


async def _attach_live_status(camera_rows: list[dict]) -> list[dict]:
    statuses = await asyncio.gather(*[_live_camera_status(row) for row in camera_rows])
    for row, status in zip(camera_rows, statuses):
        row["status"] = status
    return camera_rows


async def _fetch_edge_camera_stats() -> dict[str, dict]:
    """Single call to the edge that returns {cam_id: {width, height, fps}}.

    Returns an empty dict if the edge is unreachable so the cameras page
    still renders (the UI shows "—" for missing values).
    """
    try:
        resp = await _edge_client.get(
            f"{settings.edge_http_base_url}/camera_stats",
            timeout=2.0,
        )
        if resp.status_code != 200:
            return {}
        return resp.json() or {}
    except (httpx.HTTPError, ValueError):
        return {}


@router.get("/cameras")
async def list_cameras():
    db = await get_db()
    rows = await db.fetch("SELECT * FROM cameras ORDER BY camera_id")
    cameras = [dict(r) for r in rows]
    for camera in cameras:
        camera["source_url"] = normalize_rtsp_url(camera.get("source_url"))
    stats = await _fetch_edge_camera_stats()
    for camera in cameras:
        s = stats.get(camera["camera_id"]) or {}
        camera["width"] = s.get("width")
        camera["height"] = s.get("height")
        camera["fps"] = s.get("fps")
    return await _attach_live_status(cameras)


@router.get("/cameras/{camera_id}")
async def get_camera(camera_id: str):
    db = await get_db()
    row = await db.fetchrow("SELECT * FROM cameras WHERE camera_id = $1", camera_id)
    if row is None:
        raise HTTPException(404, "Camera not found")
    camera = dict(row)
    camera["source_url"] = normalize_rtsp_url(camera.get("source_url"))
    camera["status"] = await _live_camera_status(camera)
    return camera


@router.post("/cameras")
async def create_camera(request: Request, data: dict = Body(...)):
    db = await get_db()
    source_url = normalize_rtsp_url(data["source_url"])
    await db.execute(
        """INSERT INTO cameras (camera_id, name, source_url, assigned_edge)
           VALUES ($1, $2, $3, $4)""",
        data["camera_id"],
        data.get("name"),
        source_url,
        data.get("assigned_edge"),
    )
    edge_publisher = getattr(request.app.state, "edge_publisher", None)
    if edge_publisher and data.get("assigned_edge"):
        edge_publisher.publish_assign(data["assigned_edge"], {
            "action": "add",
            "camera_id": data["camera_id"],
            "source_url": source_url,
            "zones": [],
        })
    return {"status": "created", "camera_id": data["camera_id"]}


@router.delete("/cameras/{camera_id}")
async def delete_camera(request: Request, camera_id: str):
    db = await get_db()
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT assigned_edge FROM cameras WHERE camera_id = $1",
                camera_id,
            )
            if row is None:
                raise HTTPException(404, "Camera not found")

            linked_zone_rows = await conn.fetch(
                "SELECT zone_id FROM zones WHERE camera_id = $1 ORDER BY zone_id",
                camera_id,
            )
            linked_zone_ids = [z["zone_id"] for z in linked_zone_rows]

            if linked_zone_ids:
                await conn.execute("DELETE FROM zones WHERE camera_id = $1", camera_id)

            await conn.execute("DELETE FROM cameras WHERE camera_id = $1", camera_id)

    edge_publisher = getattr(request.app.state, "edge_publisher", None)
    if edge_publisher and row["assigned_edge"]:
        edge_publisher.publish_assign(row["assigned_edge"], {
            "action": "remove",
            "camera_id": camera_id,
        })
    return {
        "status": "deleted",
        "camera_id": camera_id,
        "deleted_zone_count": len(linked_zone_ids),
        "deleted_zone_ids": linked_zone_ids,
    }


@router.post("/cameras/{camera_id}/reboot")
async def reboot_camera(camera_id: str, request: Request):
    """Manual trigger for an ONVIF SystemReboot on the named camera.

    Bypasses the watchdog's cooldown/daily-cap logic — meant for operator-
    initiated recovery and for testing the reboot path before relying on
    the auto-watchdog. The camera goes offline for ~75-180s while it
    boots; the edge's existing RTSP retry picks it up automatically once
    the stream is back.
    """
    db = await get_db()
    row = await db.fetchrow(
        "SELECT camera_id, source_url FROM cameras WHERE camera_id = $1",
        camera_id,
    )
    if row is None:
        raise HTTPException(404, "Camera not found")
    source_url = row["source_url"]
    if not source_url:
        raise HTTPException(400, "Camera has no source_url; can't extract ONVIF credentials")

    # Reuse the watchdog's reboot routine so test path and prod path stay
    # behaviourally identical.
    wd = getattr(request.app.state, "camera_watchdog", None)
    if wd is None:
        raise HTTPException(500, "Camera watchdog not initialised")
    ok = await wd._reboot_camera(camera_id, source_url)
    if not ok:
        raise HTTPException(502, "Camera did not accept ONVIF reboot (unreachable or no ONVIF support)")
    return {
        "status": "rebooting",
        "camera_id": camera_id,
        "expected_recovery_s": 90,
        "note": "Edge will auto-pick the camera back up via its 30-s retry loop.",
    }


@router.get("/cameras/{camera_id}/snapshot")
async def get_snapshot(camera_id: str):
    return await _proxy_edge(f"/snapshot/{camera_id}", "Snapshot unavailable")


@router.get("/cameras/{camera_id}/annotated")
async def get_annotated(camera_id: str):
    return await _proxy_edge(f"/annotated/{camera_id}", "Annotated frame unavailable")


@router.get("/cameras/{camera_id}/stream")
async def get_stream(camera_id: str):
    edge_url = f"{settings.edge_http_base_url}/stream/{camera_id}"

    async def _relay():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", edge_url) as resp:
                    if resp.status_code != 200:
                        raise HTTPException(503, "Live stream unavailable")
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk
        except httpx.ConnectError:
            raise HTTPException(503, "Edge HTTP server is not running")

    return StreamingResponse(
        _relay(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
