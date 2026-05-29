"""Zones API: CRUD + metric history + live metrics."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, HTTPException, Body, Request

from cloud.models.db import get_db, get_redis

router = APIRouter(prefix="/api", tags=["zones"])


def _zone_write_error(exc: Exception, *, zone_id: str) -> HTTPException | None:
    """Translate asyncpg constraint errors into user-readable 4xx responses."""
    if isinstance(exc, asyncpg.exceptions.UniqueViolationError):
        return HTTPException(409, f"A zone with ID '{zone_id}' already exists. Pick a different Zone ID.")
    if isinstance(exc, asyncpg.exceptions.ForeignKeyViolationError):
        name = getattr(exc, "constraint_name", "") or ""
        if "camera" in name:
            return HTTPException(400, "Selected camera does not exist. Add it under Cameras first, then retry.")
        if "zone_group" in name:
            return HTTPException(400, "Selected zone group does not exist. Pick a valid group or leave it blank.")
        return HTTPException(400, f"Foreign key violation: {name}")
    if isinstance(exc, asyncpg.exceptions.CheckViolationError):
        name = getattr(exc, "constraint_name", "") or ""
        if "ramp_type" in name:
            return HTTPException(400, "Ramp type must be 'inner' or 'outer' (or left blank).")
        return HTTPException(400, f"Check constraint failed: {name}")
    if isinstance(exc, asyncpg.exceptions.NotNullViolationError):
        col = getattr(exc, "column_name", "") or ""
        return HTTPException(400, f"Missing required field: {col or 'unknown'}")
    return None


def _parse_zone_row(row) -> dict:
    data = dict(row)
    zone_poly = data.get("zone_poly")
    if isinstance(zone_poly, str):
        try:
            data["zone_poly"] = json.loads(zone_poly)
        except json.JSONDecodeError:
            pass
    return data


async def _camera_sync_payload(camera_id: str) -> dict | None:
    db = await get_db()
    camera = await db.fetchrow(
        "SELECT camera_id, source_url, assigned_edge FROM cameras WHERE camera_id = $1",
        camera_id,
    )
    if camera is None:
        return None

    zones = await db.fetch(
        """
        SELECT zone_id, camera_id, name, zone_poly, ramp_type, max_vehicles, max_dwell_time_s
        FROM zones
        WHERE camera_id = $1 AND active = TRUE
        ORDER BY zone_id
        """,
        camera_id,
    )

    zone_payloads = []
    for zone in zones:
        zone_dict = _parse_zone_row(zone)
        zone_payloads.append({
            "zone_id": zone_dict["zone_id"],
            "camera_id": zone_dict["camera_id"],
            "name": zone_dict["name"],
            "zone_poly": zone_dict["zone_poly"],
            "ramp_type": zone_dict.get("ramp_type"),
            "max_vehicles": zone_dict["max_vehicles"],
            "max_dwell_time_s": zone_dict["max_dwell_time_s"],
        })

    return {
        "edge_id": camera["assigned_edge"],
        "assign_data": {
            "action": "sync",
            "camera_id": camera["camera_id"],
            "source_url": camera["source_url"],
            "zones": zone_payloads,
        },
    }


async def _publish_camera_sync(request: Request, camera_id: str) -> None:
    payload = await _camera_sync_payload(camera_id)
    if payload is None or not payload["edge_id"]:
        return

    edge_publisher = getattr(request.app.state, "edge_publisher", None)
    if edge_publisher is None:
        return

    edge_publisher.publish_assign(payload["edge_id"], payload["assign_data"])


@router.get("/zones")
async def list_zones():
    db = await get_db()
    # Join zone_groups so the dashboard's "Map slot" column can show
    # which floor-plan slot each zone feeds into without a second roundtrip.
    rows = await db.fetch(
        """SELECT z.*, g.name AS group_name, g.max_vehicles AS group_max_vehicles
           FROM zones z
           LEFT JOIN zone_groups g ON g.group_id = z.zone_group_id
           ORDER BY z.zone_id"""
    )
    return [_parse_zone_row(r) for r in rows]


@router.get("/zones/{zone_id}")
async def get_zone(zone_id: str):
    db = await get_db()
    row = await db.fetchrow("SELECT * FROM zones WHERE zone_id = $1", zone_id)
    if row is None:
        raise HTTPException(404, "Zone not found")
    return _parse_zone_row(row)


@router.post("/zones")
async def create_zone(request: Request, data: dict = Body(...)):
    db = await get_db()
    zone_id = (data.get("zone_id") or "").strip()
    if not zone_id:
        raise HTTPException(400, "Zone ID is required.")
    try:
        await db.execute(
            """INSERT INTO zones (zone_id, name, camera_id, zone_group_id, zone_poly, ramp_type, max_vehicles, max_dwell_time_s)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)""",
            zone_id, data.get("name", zone_id),
            data.get("camera_id"),
            data.get("zone_group_id"),
            json.dumps(data.get("zone_poly")) if data.get("zone_poly") else None,
            data.get("ramp_type"),
            data.get("max_vehicles", 20),
            data.get("max_dwell_time_s", 900.0),
        )
    except asyncpg.exceptions.PostgresError as e:
        translated = _zone_write_error(e, zone_id=zone_id)
        if translated is not None:
            raise translated from e
        raise
    if data.get("camera_id"):
        await _publish_camera_sync(request, data["camera_id"])
    return {"status": "created", "zone_id": zone_id}


@router.put("/zones/{zone_id}")
async def update_zone(request: Request, zone_id: str, data: dict = Body(...)):
    db = await get_db()
    row = await db.fetchrow("SELECT * FROM zones WHERE zone_id = $1", zone_id)
    if row is None:
        raise HTTPException(404, "Zone not found")
    old_camera_id = row["camera_id"]
    new_camera_id = data["camera_id"] if "camera_id" in data else old_camera_id
    # zone_group_id treated separately: PATCH-style — only update if the
    # client passed the key (allows explicit clearing with null) so that
    # routine updates from the basic zone editor don't accidentally orphan
    # a zone out of its group.
    set_group_id = "zone_group_id" in data
    try:
        await db.execute(
            f"""UPDATE zones SET
               name = COALESCE($2, name),
               zone_poly = COALESCE($3::jsonb, zone_poly),
               ramp_type = COALESCE($4, ramp_type),
               max_vehicles = COALESCE($5, max_vehicles),
               max_dwell_time_s = COALESCE($6, max_dwell_time_s),
               camera_id = $7,
               active = COALESCE($8, active),
               {"zone_group_id = $9," if set_group_id else ""}
               updated_at = NOW()
               WHERE zone_id = $1""",
            zone_id,
            data.get("name"),
            json.dumps(data.get("zone_poly")) if data.get("zone_poly") else None,
            data.get("ramp_type"),
            data.get("max_vehicles"),
            data.get("max_dwell_time_s"),
            new_camera_id,
            data.get("active"),
            *([data.get("zone_group_id")] if set_group_id else []),
        )
    except asyncpg.exceptions.PostgresError as e:
        translated = _zone_write_error(e, zone_id=zone_id)
        if translated is not None:
            raise translated from e
        raise
    if old_camera_id and old_camera_id != new_camera_id:
        await _publish_camera_sync(request, old_camera_id)
    if new_camera_id:
        await _publish_camera_sync(request, new_camera_id)
    return {"status": "updated"}


@router.delete("/zones/{zone_id}")
async def delete_zone(request: Request, zone_id: str):
    db = await get_db()
    row = await db.fetchrow("SELECT camera_id FROM zones WHERE zone_id = $1", zone_id)
    if row is None:
        raise HTTPException(404, "Zone not found")
    await db.execute("DELETE FROM zones WHERE zone_id = $1", zone_id)
    if row["camera_id"]:
        await _publish_camera_sync(request, row["camera_id"])
    return {"status": "deleted"}


@router.get("/zones/{zone_id}/live")
async def zone_live(zone_id: str):
    """Return the latest cached metrics from Redis."""
    redis = await get_redis()
    data = await redis.hgetall(f"vzone:{zone_id}:latest")
    if not data:
        raise HTTPException(404, "No live data for zone")
    # Parse JSON fields
    if "vehicle_count_by_type" in data:
        data["vehicle_count_by_type"] = json.loads(data["vehicle_count_by_type"])
    if "overstay_alert_ids" in data:
        data["overstay_alert_ids"] = json.loads(data["overstay_alert_ids"])
    return data


@router.get("/zones/{zone_id}/history")
async def zone_history(zone_id: str, minutes: int = 60, resolution: str = "1m"):
    """Return time-series metric history from TimescaleDB."""
    db = await get_db()
    if resolution == "raw":
        table = "vehicle_zone_metrics"
        ts_col = "ts"
    elif resolution == "1h":
        table = "vehicle_zone_metrics_1h"
        ts_col = "bucket"
    else:
        table = "vehicle_zone_metrics_1m"
        ts_col = "bucket"

    rows = await db.fetch(
        f"SELECT * FROM {table} WHERE zone_id = $1 AND {ts_col} >= NOW() - INTERVAL '{minutes} minutes' ORDER BY {ts_col}",
        zone_id,
    )
    results = []
    for r in rows:
        row_dict = dict(r)
        for k, v in row_dict.items():
            if isinstance(v, datetime):
                row_dict[k] = v.isoformat()
        results.append(row_dict)
    return results
