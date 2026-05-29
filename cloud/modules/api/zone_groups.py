"""Zone-groups API: aggregate N camera-zones into one logical zone.

Operations side often has a *physical* area (e.g. "Zone 5" on the arrival
ramp) that several cameras cover from different angles. The existing
zones table already supports many zones per camera; this module adds the
inverse — one logical zone composed of many camera-zone rows — without
disturbing the single-camera-zone case (zones with zone_group_id IS NULL
behave exactly as before).

What this layer aggregates:
  * vehicle_count = SUM of member zones' live counts
  * max_dwell_time_s = MAX across members
  * overstay_count = SUM across members

These are *summed counts*, not unique-vehicle counts — a car visible to
two cameras shows up in both rows. Cross-camera dedup needs camera
calibration or plate OCR, neither of which is in scope here. The
dashboard labels group totals as "sum across N feeds" so operators know
what they're reading.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Query

from cloud.models.db import get_db, get_redis

router = APIRouter(prefix="/api", tags=["zone_groups"])


def _row_to_dict(row) -> dict:
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


@router.get("/zone_groups")
async def list_zone_groups():
    db = await get_db()
    rows = await db.fetch(
        """
        SELECT g.group_id, g.name, g.max_vehicles, g.max_dwell_time_s,
               g.created_at, g.updated_at,
               (SELECT COUNT(*) FROM zones z WHERE z.zone_group_id = g.group_id) AS zone_count
        FROM zone_groups g
        ORDER BY g.name
        """
    )
    return [_row_to_dict(r) for r in rows]


@router.post("/zone_groups")
async def create_zone_group(data: dict = Body(...)):
    group_id = (data.get("group_id") or "").strip()
    name = (data.get("name") or "").strip()
    if not group_id or not name:
        raise HTTPException(400, "group_id and name are required")
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO zone_groups (group_id, name, max_vehicles, max_dwell_time_s)
            VALUES ($1, $2, $3, $4)
            """,
            group_id, name, data.get("max_vehicles"), data.get("max_dwell_time_s"),
        )
    except Exception as e:
        # asyncpg raises UniqueViolationError on duplicate PK; surface a clean 409.
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"Zone group '{group_id}' already exists")
        raise HTTPException(500, f"Failed to create zone group: {e}")
    return {"status": "created", "group_id": group_id}


@router.get("/zone_groups/{group_id}")
async def get_zone_group(group_id: str):
    db = await get_db()
    row = await db.fetchrow(
        "SELECT * FROM zone_groups WHERE group_id = $1", group_id,
    )
    if row is None:
        raise HTTPException(404, "Zone group not found")
    members = await db.fetch(
        """
        SELECT zone_id, name, camera_id, max_vehicles, max_dwell_time_s
        FROM zones WHERE zone_group_id = $1
        ORDER BY zone_id
        """,
        group_id,
    )
    result = _row_to_dict(row)
    result["zones"] = [_row_to_dict(m) for m in members]
    return result


@router.put("/zone_groups/{group_id}")
async def update_zone_group(group_id: str, data: dict = Body(...)):
    db = await get_db()
    res = await db.execute(
        """
        UPDATE zone_groups
        SET name = COALESCE($2, name),
            max_vehicles = COALESCE($3, max_vehicles),
            max_dwell_time_s = COALESCE($4, max_dwell_time_s),
            updated_at = NOW()
        WHERE group_id = $1
        """,
        group_id, data.get("name"), data.get("max_vehicles"), data.get("max_dwell_time_s"),
    )
    if res.endswith("0"):
        raise HTTPException(404, "Zone group not found")
    return {"status": "updated", "group_id": group_id}


@router.delete("/zone_groups/{group_id}")
async def delete_zone_group(group_id: str):
    db = await get_db()
    # ON DELETE SET NULL on the FK means member zones survive with their
    # zone_group_id cleared — operator can re-assign or leave standalone.
    res = await db.execute(
        "DELETE FROM zone_groups WHERE group_id = $1", group_id,
    )
    if res.endswith("0"):
        raise HTTPException(404, "Zone group not found")
    return {"status": "deleted", "group_id": group_id}


@router.get("/zone_groups/{group_id}/live")
async def get_zone_group_live(group_id: str):
    """Aggregated live metrics for a zone group.

    Reads the per-zone live hashes from Redis (the same data the
    Overview cards already render for individual zones) and sums them.
    Returns the member list too so the operator can see what's being
    aggregated.

    Caveat — `vehicle_count` is a SUM across feeds, not a unique
    vehicle count. A car parked between two cameras' overlapping views
    is counted in both. The dashboard labels this explicitly.
    """
    db = await get_db()
    group = await db.fetchrow(
        "SELECT * FROM zone_groups WHERE group_id = $1", group_id,
    )
    if group is None:
        raise HTTPException(404, "Zone group not found")
    member_rows = await db.fetch(
        "SELECT zone_id, name, camera_id FROM zones WHERE zone_group_id = $1",
        group_id,
    )

    redis = await get_redis()
    total_count = 0
    total_overstay = 0
    max_dwell = 0.0
    members = []
    for m in member_rows:
        zid = m["zone_id"]
        live = await redis.hgetall(f"vzone:{zid}:latest")
        # Redis hgetall on a missing key returns {}; treat as zero.
        v_count = int(float(live.get("vehicle_count", 0) or 0))
        o_count = int(float(live.get("overstay_count", 0) or 0))
        m_dwell = float(live.get("max_dwell_time_s", 0) or 0)
        total_count += v_count
        total_overstay += o_count
        if m_dwell > max_dwell:
            max_dwell = m_dwell
        members.append({
            "zone_id": zid,
            "name": m["name"],
            "camera_id": m["camera_id"],
            "vehicle_count": v_count,
            "overstay_count": o_count,
            "max_dwell_time_s": m_dwell,
        })

    return {
        "group_id": group_id,
        "name": group["name"],
        "max_vehicles": group["max_vehicles"],
        "max_dwell_time_s": group["max_dwell_time_s"],
        "member_count": len(members),
        "vehicle_count": total_count,
        "overstay_count": total_overstay,
        "max_dwell_observed_s": max_dwell,
        "members": members,
    }
