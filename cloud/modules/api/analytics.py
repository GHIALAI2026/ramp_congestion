"""Analytics API: time-bucketed alert counts for the dashboard chart."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query

from cloud.models.db import get_db

router = APIRouter(prefix="/api", tags=["analytics"])


# Step durations and label formats per granularity.
STEP_SECONDS = {"10min": 600, "hour": 3600, "day": 86400}

# Range key -> spec. `anchor`:
#   * "calendar_day" — start aligns to local midnight (or N days before midnight).
#   * "rolling"      — end aligns to the current bucket boundary; start is N buckets earlier.
RANGE_SPECS = {
    "1h":  {"granularity": "10min", "buckets": 6,  "anchor": "rolling",      "label_fmt": "%H:%M"},
    "24h": {"granularity": "hour",  "buckets": 24, "anchor": "rolling",      "label_fmt": "%H:00"},
    "7d":  {"granularity": "day",   "buckets": 7,  "anchor": "calendar_day", "label_fmt": "%a %d %b"},
    "30d": {"granularity": "day",   "buckets": 30, "anchor": "calendar_day", "label_fmt": "%d %b"},
}


def _snap_down(dt: datetime, granularity: str) -> datetime:
    if granularity == "10min":
        return dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)
    if granularity == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt


@router.get("/analytics/alerts")
async def alert_buckets(
    range_key: str = Query("24h", alias="range", description="1h | 24h | 7d | 30d"),
    alert_type: str = Query("overcrowding", alias="type", description="overcrowding | overstay"),
    tz: str = Query("UTC", description="IANA timezone name (e.g. Asia/Kolkata) used to align bucket boundaries"),
    vehicles_offset: int = Query(0, ge=0, description="Pagination offset for top_vehicles list"),
    vehicles_limit: int = Query(10, ge=1, le=100, description="Pagination page size for top_vehicles"),
    zones_offset: int = Query(0, ge=0, description="Pagination offset for top_zones list"),
    zones_limit: int = Query(10, ge=1, le=100, description="Pagination page size for top_zones"),
    zone_id: str | None = Query(None, description="If set, restrict chart + panel data to this zone."),
):
    spec = RANGE_SPECS.get(range_key)
    if spec is None:
        raise HTTPException(400, f"Unknown range '{range_key}'. Use one of: {list(RANGE_SPECS)}")
    if alert_type not in ("overcrowding", "overstay"):
        raise HTTPException(400, "type must be 'overcrowding' or 'overstay'")

    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, f"Invalid timezone '{tz}'")

    granularity = spec["granularity"]
    buckets_n = spec["buckets"]
    anchor = spec["anchor"]
    step_seconds = STEP_SECONDS[granularity]
    label_fmt = spec["label_fmt"]

    db = await get_db()

    # All time math happens in the user's local timezone so labels line up
    # with their calendar/clock. start/end remain tz-aware so SQL comparisons
    # against TIMESTAMPTZ columns stay correct.
    now_local = datetime.now(zone)
    if anchor == "rolling":
        # Snap current time DOWN to the bucket boundary, then walk back N-1 buckets.
        # The current (partially elapsed) bucket sits at the right edge of the chart.
        snapped = _snap_down(now_local, granularity)
        start = snapped - timedelta(seconds=step_seconds * (buckets_n - 1))
        end = snapped + timedelta(seconds=step_seconds)
    else:
        # Calendar-day anchored: start at local midnight (or N days earlier).
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        if granularity == "hour":
            start = midnight
            end = start + timedelta(days=1)
        else:
            start = midnight - timedelta(days=buckets_n - 1)
            end = start + timedelta(days=buckets_n)

    # Bucket by integer offset from `start` to dodge session-timezone weirdness
    # in date_trunc; epoch math is unambiguous. Optional zone_id filter is
    # injected via "(zone_id = $N OR $N IS NULL)" pattern so we don't have to
    # branch the SQL.
    #
    # Count distinct VISITS, not raw alert rows. Before the runtime dedup
    # landed (cloud/modules/ingestion/mqtt_consumer.py), an overstaying
    # vehicle produced ~10 alert rows per hour as the edge re-fired every
    # 5 min. Counting those as separate events inflated the chart by ~10×
    # for hours that had any long overstays. We use a window-function LAG
    # over (zone_id, track_id) to group contiguous bursts into one visit;
    # gaps > 15 minutes start a new visit. The same logic applies to
    # overcrowding (where track_id is NULL — all NULLs partition together
    # per zone, so the same query catches restart-induced overcrowding
    # bursts too).
    rows = await db.fetch(
        """
        WITH visit_starts AS (
          SELECT ts FROM (
            SELECT ts,
                   LAG(ts) OVER (PARTITION BY zone_id, track_id ORDER BY ts) AS prev_ts
            FROM vehicle_alerts
            WHERE alert_type = $1 AND ts >= $2 AND ts < $3
              AND ($5::text IS NULL OR zone_id = $5)
          ) t
          WHERE prev_ts IS NULL OR (ts - prev_ts) > INTERVAL '15 minutes'
        )
        SELECT FLOOR(EXTRACT(EPOCH FROM (ts - $2)) / $4)::int AS bucket_idx,
               COUNT(*) AS n
        FROM visit_starts
        GROUP BY 1
        ORDER BY 1
        """,
        alert_type, start, end, step_seconds, zone_id,
    )
    counts_by_idx: dict[int, int] = {int(r["bucket_idx"]): int(r["n"]) for r in rows}

    # Build a continuous list so the chart renders zeros too.
    buckets = []
    step = timedelta(seconds=step_seconds)
    for i in range(buckets_n):
        ts_b = start + step * i
        buckets.append({
            "ts": ts_b.isoformat(),
            "label": ts_b.strftime(label_fmt),
            "count": counts_by_idx.get(i, 0),
        })

    total = sum(b["count"] for b in buckets)

    # For overcrowding: paginated list of zones by alert count.
    # For overstay: paginated list of vehicles by their longest dwell time.
    top_zones: list[dict] = []
    top_vehicles: list[dict] = []
    total_zones: int = 0
    total_vehicles: int = 0
    if alert_type == "overcrowding":
        total_zones = await db.fetchval(
            """
            SELECT COUNT(DISTINCT zone_id)
            FROM vehicle_alerts
            WHERE alert_type = $1 AND ts >= $2 AND ts < $3
              AND ($4::text IS NULL OR zone_id = $4)
            """,
            alert_type, start, end, zone_id,
        ) or 0
        # Per-zone count uses the same visit-dedup logic as the chart so
        # a single 90-minute overcrowding burst stops reading as "9 alerts".
        rows = await db.fetch(
            """
            WITH visit_starts AS (
              SELECT zone_id FROM (
                SELECT zone_id, ts,
                       LAG(ts) OVER (PARTITION BY zone_id, track_id ORDER BY ts) AS prev_ts
                FROM vehicle_alerts a
                WHERE a.alert_type = $1 AND a.ts >= $2 AND a.ts < $3
                  AND ($6::text IS NULL OR a.zone_id = $6)
              ) t
              WHERE prev_ts IS NULL OR (ts - prev_ts) > INTERVAL '15 minutes'
            )
            SELECT vs.zone_id,
                   COALESCE(z.name, vs.zone_id) AS zone_name,
                   COUNT(*) AS n
            FROM visit_starts vs
            LEFT JOIN zones z ON z.zone_id = vs.zone_id
            GROUP BY vs.zone_id, z.name
            ORDER BY n DESC
            LIMIT $4 OFFSET $5
            """,
            alert_type, start, end, zones_limit, zones_offset, zone_id,
        )
        top_zones = [
            {"zone_id": r["zone_id"], "zone_name": r["zone_name"], "count": int(r["n"])}
            for r in rows
        ]
    else:  # overstay
        total_vehicles = await db.fetchval(
            """
            SELECT COUNT(*) FROM (
              SELECT 1
              FROM vehicle_alerts a
              WHERE a.alert_type = $1 AND a.ts >= $2 AND a.ts < $3
                AND a.track_id IS NOT NULL
                AND a.dwell_time_s IS NOT NULL
                AND ($4::text IS NULL OR a.zone_id = $4)
              GROUP BY a.track_id, a.zone_id
            ) t
            """,
            alert_type, start, end, zone_id,
        ) or 0
        # Pick the row with the highest dwell per (track_id, zone_id) and
        # return its full context (alert_id, ts, image_url) so the UI can
        # show when it happened and link directly to the evidence image —
        # a bare "Vehicle #29" is meaningless on its own.
        rows = await db.fetch(
            """
            WITH ranked AS (
              SELECT a.track_id,
                     a.zone_id,
                     a.camera_id,
                     a.alert_id,
                     a.ts,
                     a.image_url,
                     a.dwell_time_s,
                     COALESCE(z.name, a.zone_id) AS zone_name,
                     ROW_NUMBER() OVER (
                       PARTITION BY a.track_id, a.zone_id
                       ORDER BY a.dwell_time_s DESC NULLS LAST, a.ts DESC
                     ) AS rn
              FROM vehicle_alerts a
              LEFT JOIN zones z ON z.zone_id = a.zone_id
              WHERE a.alert_type = $1 AND a.ts >= $2 AND a.ts < $3
                AND a.track_id IS NOT NULL
                AND a.dwell_time_s IS NOT NULL
                AND ($6::text IS NULL OR a.zone_id = $6)
            )
            SELECT track_id, zone_id, zone_name, camera_id,
                   alert_id, ts, image_url, dwell_time_s
            FROM ranked
            WHERE rn = 1
            ORDER BY dwell_time_s DESC NULLS LAST
            LIMIT $4 OFFSET $5
            """,
            alert_type, start, end, vehicles_limit, vehicles_offset, zone_id,
        )
        top_vehicles = [
            {
                "track_id": int(r["track_id"]),
                "zone_id": r["zone_id"],
                "zone_name": r["zone_name"],
                "camera_id": r["camera_id"],
                "alert_id": int(r["alert_id"]) if r["alert_id"] is not None else None,
                "ts": r["ts"].isoformat() if r["ts"] is not None else None,
                "image_url": r["image_url"],
                "dwell_time_s": float(r["dwell_time_s"]) if r["dwell_time_s"] is not None else None,
            }
            for r in rows
        ]

    # Distinct zones that have ANY alert of this type in the window — drives
    # the dropdown options. Always derived against the unfiltered window so a
    # selection doesn't shrink its own option list.
    zone_opts_rows = await db.fetch(
        """
        SELECT DISTINCT a.zone_id, COALESCE(z.name, a.zone_id) AS zone_name
        FROM vehicle_alerts a
        LEFT JOIN zones z ON z.zone_id = a.zone_id
        WHERE a.alert_type = $1 AND a.ts >= $2 AND a.ts < $3
        ORDER BY zone_name
        """,
        alert_type, start, end,
    )
    zones_with_alerts = [
        {"zone_id": r["zone_id"], "zone_name": r["zone_name"]}
        for r in zone_opts_rows
    ]

    return {
        "range": range_key,
        "type": alert_type,
        "granularity": granularity,
        "anchor": anchor,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "buckets": buckets,
        "total": total,
        "top_zones": top_zones,
        "top_vehicles": top_vehicles,
        "vehicles_offset": vehicles_offset,
        "vehicles_limit": vehicles_limit,
        "vehicles_total": int(total_vehicles),
        "zones_offset": zones_offset,
        "zones_limit": zones_limit,
        "zones_total": int(total_zones),
        "zone_id": zone_id,
        "zones_with_alerts": zones_with_alerts,
    }
