"""Overview API: aggregated dashboard view."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter

from cloud.models.db import get_db, get_redis

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
async def get_overview():
    """Return aggregated metrics across all active zones."""
    db = await get_db()
    redis = await get_redis()

    # Get all zones from DB along with their group's metadata (LEFT JOIN
    # so ungrouped zones still come through with NULL group_*). The
    # Overview page client uses group_id + group_name + group_max_vehicles
    # to collapse member zones into one "logical zone" row per group.
    zones = await db.fetch(
        """
        SELECT z.zone_id, z.name, z.camera_id, z.ramp_type,
               z.max_vehicles, z.max_dwell_time_s,
               z.zone_group_id,
               g.name AS group_name,
               g.max_vehicles AS group_max_vehicles
        FROM zones z
        LEFT JOIN zone_groups g ON g.group_id = z.zone_group_id
        WHERE z.active = TRUE
        ORDER BY z.zone_id
        """
    )

    zone_summaries = []
    total_vehicles = 0
    overcrowding_count = 0
    total_overstay = 0

    for z in zones:
        zone_id = z["zone_id"]
        live = await redis.hgetall(f"vzone:{zone_id}:latest")
        vc = int(live.get("vehicle_count", 0))
        total_vehicles += vc

        overcrowding = live.get("overcrowding_alert", "False") == "True"
        if overcrowding:
            overcrowding_count += 1

        overstay_count = int(live.get("overstay_count", 0))
        total_overstay += overstay_count

        zone_summaries.append({
            "zone_id": zone_id,
            "name": z["name"],
            "camera_id": z["camera_id"],
            "ramp_type": z["ramp_type"],
            "vehicle_count": vc,
            "max_vehicles": z["max_vehicles"],
            "dwell_threshold_s": float(z["max_dwell_time_s"] or 0),
            "occupancy_pct": float(live.get("occupancy_pct", 0)),
            "overcrowding_alert": overcrowding,
            "overstay_count": overstay_count,
            "avg_dwell_time_s": float(live.get("avg_dwell_time_s", 0)),
            "max_dwell_time_s": float(live.get("max_dwell_time_s", 0)),
            "zone_group_id": z["zone_group_id"],
            "group_name": z["group_name"],
            "group_max_vehicles": z["group_max_vehicles"],
        })

    # Recent alert count
    alert_count = await db.fetchval(
        "SELECT COUNT(*) FROM vehicle_alerts WHERE acknowledged = FALSE AND ts > NOW() - INTERVAL '1 hour'"
    ) or 0

    # Edge status
    edge_keys = []
    cursor = b"0"
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match="vedge:*:heartbeat")
        edge_keys.extend(keys)
        if cursor == 0 or cursor == b"0":
            break

    edges = []
    for key in edge_keys:
        data = await redis.hgetall(key)
        if data:
            edges.append({
                "edge_id": data.get("edge_id"),
                "ts": float(data.get("ts", 0)),
                "uptime_s": float(data.get("uptime_s", 0)),
                "cameras_active": int(data.get("cameras_active", 0)),
                "cameras_assigned": int(data.get("cameras_assigned", 0)),
                "cameras_errored": int(data.get("cameras_errored", 0)),
                "zones_active": int(data.get("zones_active", 0)),
                "cpu_pct": float(data.get("cpu_pct", 0)),
                "mem_pct": float(data.get("mem_pct", 0)),
                "gpu_pct": float(data.get("gpu_pct", 0)) if data.get("gpu_pct") else None,
                "gpu_mem_pct": float(data.get("gpu_mem_pct", 0)) if data.get("gpu_mem_pct") else None,
                "gpu_name": data.get("gpu_name"),
                "gpu_note": data.get("gpu_note"),
                "gpu_temp_c": float(data.get("gpu_temp_c", 0)) if data.get("gpu_temp_c") else None,
                "gpu_mem_used_mb": float(data.get("gpu_mem_used_mb", 0)) if data.get("gpu_mem_used_mb") else None,
                "gpu_mem_total_mb": float(data.get("gpu_mem_total_mb", 0)) if data.get("gpu_mem_total_mb") else None,
                "gpu_active_cores": int(data.get("gpu_active_cores", 0)) if data.get("gpu_active_cores") else None,
                "gpu_total_cores": int(data.get("gpu_total_cores", 0)) if data.get("gpu_total_cores") else None,
                "igpu_name": data.get("igpu_name"),
                "igpu_pct": float(data.get("igpu_pct", 0)) if data.get("igpu_pct") else None,
                "igpu_video_pct": float(data.get("igpu_video_pct", 0)) if data.get("igpu_video_pct") else None,
                "igpu_video_enhance_pct": float(data.get("igpu_video_enhance_pct", 0)) if data.get("igpu_video_enhance_pct") else None,
                "igpu_note": data.get("igpu_note"),
                "inference_fps": float(data.get("inference_fps", 0)) if data.get("inference_fps") else None,
                "inference_ms": float(data.get("inference_ms", 0)) if data.get("inference_ms") else None,
                "error_cameras": json.loads(data.get("error_cameras", "[]")),
            })

    return {
        "total_vehicles": total_vehicles,
        "total_zones": len(zones),
        "overcrowding_zones": overcrowding_count,
        "total_overstay": total_overstay,
        "unacked_alerts": alert_count,
        "zones": zone_summaries,
        "edges": edges,
    }
