"""Alerts API: list and acknowledge vehicle alerts."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Body, Query

from cloud.models.db import get_db

router = APIRouter(prefix="/api", tags=["alerts"])


@router.get("/alerts")
async def list_alerts(
    zone_id: list[str] | None = Query(None),
    alert_type: str | None = None,
    level: str | None = None,
    since_hours: float | None = Query(None, ge=0),
    unacked_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
):
    """Return {alerts, counts}.

    `counts` reflects the full DB matching the zone/type/level/time filters (it
    ignores `unacked_only` deliberately) so the dashboard's stat cards survive
    page reloads and aren't misled by the unacked-only list filter, which would
    otherwise force the "acknowledged" count to read 0. The counts are computed
    in SQL across the whole table, NOT from the (limit-capped) returned list, so
    a zone/severity filter narrows them to a true full-DB total rather than a
    slice of the most recent `limit` rows.
      - critical / warning: unacked at this level (operator backlog)
      - acknowledged:       acked rows whose acked_at is today (server-local
                            calendar day). Bounded — a lifetime counter
                            would just grow without operational meaning.
      - total:              size of the returned list (matches what's visible)

    `zone_id` is repeatable: a dashboard zone filter is a friendly *label* that
    may map to several zone_ids (a zone group), so the client sends every id the
    label resolves to and we match with ANY().
    """
    db = await get_db()
    list_conditions: list[str] = []
    count_conditions: list[str] = []
    params: list = []
    i = 1

    if zone_id:
        clause = f"zone_id = ANY(${i})"
        list_conditions.append(clause)
        count_conditions.append(clause)
        params.append(zone_id)
        i += 1
    if alert_type:
        clause = f"alert_type = ${i}"
        list_conditions.append(clause)
        count_conditions.append(clause)
        params.append(alert_type)
        i += 1
    if level:
        clause = f"level = ${i}"
        list_conditions.append(clause)
        count_conditions.append(clause)
        params.append(level)
        i += 1
    if since_hours:
        clause = f"ts > NOW() - (${i}::double precision * INTERVAL '1 hour')"
        list_conditions.append(clause)
        count_conditions.append(clause)
        params.append(since_hours)
        i += 1
    if unacked_only:
        list_conditions.append("acknowledged = FALSE")

    list_where = (" WHERE " + " AND ".join(list_conditions)) if list_conditions else ""
    count_where = (" WHERE " + " AND ".join(count_conditions)) if count_conditions else ""

    list_query = f"SELECT * FROM vehicle_alerts{list_where} ORDER BY ts DESC LIMIT ${i}"
    rows = await db.fetch(list_query, *params, limit)

    # Single aggregation: unacked counts by level are lifetime within the
    # filter; acked count is restricted to today's calendar day on the
    # server's local clock. acked_at::date relies on the column being
    # `timestamp with time zone`, which it is in our schema.
    count_query = (
        "SELECT "
        "  SUM(CASE WHEN NOT acknowledged AND level = 'critical' THEN 1 ELSE 0 END) AS critical,"
        "  SUM(CASE WHEN NOT acknowledged AND level = 'warning' THEN 1 ELSE 0 END) AS warning,"
        "  SUM(CASE WHEN acknowledged AND acked_at::date = CURRENT_DATE THEN 1 ELSE 0 END) AS acknowledged_today "
        f"FROM vehicle_alerts{count_where}"
    )
    count_row = await db.fetchrow(count_query, *params)

    results = []
    for r in rows:
        row_dict = dict(r)
        for k, v in row_dict.items():
            if isinstance(v, datetime):
                row_dict[k] = v.isoformat()
        results.append(row_dict)

    counts = {
        "critical": int(count_row["critical"] or 0) if count_row else 0,
        "warning": int(count_row["warning"] or 0) if count_row else 0,
        "acknowledged": int(count_row["acknowledged_today"] or 0) if count_row else 0,
        "total": len(results),
    }

    return {"alerts": results, "counts": counts}


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int, data: dict = Body(default={})):
    db = await get_db()
    row = await db.fetchrow(
        "SELECT * FROM vehicle_alerts WHERE alert_id = $1", alert_id)
    if row is None:
        raise HTTPException(404, "Alert not found")
    await db.execute(
        "UPDATE vehicle_alerts SET acknowledged = TRUE, acked_by = $2, acked_at = NOW() WHERE alert_id = $1",
        alert_id, data.get("acked_by", "operator"),
    )
    return {"status": "acknowledged"}
