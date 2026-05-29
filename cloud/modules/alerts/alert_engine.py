"""Cloud alert engine for vehicle zone business rules."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis

from cloud.modules.alerts.image_capture import capture_alert_image

logger = logging.getLogger(__name__)


_INSERT_ALERT = """
INSERT INTO vehicle_alerts (
    ts, edge_id, zone_id, camera_id,
    alert_type, level, message,
    vehicle_count, dwell_time_s, track_id
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""


def _fmt_dwell(secs: float) -> str:
    """Format a duration as `Hh Mm Ss` / `Mm Ss` / `Ss` for human-readable alerts."""
    s = max(0, int(secs))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m {s % 60}s"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


@dataclass
class ZoneAlertState:
    overcrowding_started_at: float | None = None
    last_overcrowding_alert_ts: float = 0.0


@dataclass
class GroupAlertState:
    """Cooldown state for one zone-group. Mirrors ZoneAlertState shape so
    the sustain/repeat logic is identical at both scopes — operators see
    consistent timing whether an overcrowding alert came from a single
    camera-zone or from a group-level sum across feeds."""
    overcrowding_started_at: float | None = None
    last_overcrowding_alert_ts: float = 0.0


class AlertEngine:
    """Evaluates live zone state and generates cloud-side alerts."""

    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None
        self._zone_state: dict[str, ZoneAlertState] = {}
        self._group_state: dict[str, GroupAlertState] = {}
        self._overcrowding_sustain_s = 120.0
        self._overcrowding_repeat_s = 900.0  # 15 min between repeat congestion alerts per zone

    async def start(self, db: asyncpg.Pool, redis: aioredis.Redis) -> None:
        self._db = db
        self._redis = redis
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Alert engine started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Alert engine stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._evaluate_all_zones()
                await self._evaluate_all_groups()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Alert engine loop error")
                await asyncio.sleep(1.0)

    async def _evaluate_all_zones(self) -> None:
        rows = await self._db.fetch(
            """
            SELECT zone_id, name, camera_id, zone_group_id,
                   max_vehicles, max_dwell_time_s
            FROM zones
            WHERE active = TRUE
            """
        )
        now_ts = time.time()
        active_zone_ids = {row["zone_id"] for row in rows}
        self._zone_state = {
            zone_id: state
            for zone_id, state in self._zone_state.items()
            if zone_id in active_zone_ids
        }

        for row in rows:
            zone_id = row["zone_id"]
            live = await self._redis.hgetall(f"vzone:{zone_id}:latest")
            if not live:
                continue
            await self._evaluate_zone(row, live, now_ts)

    async def _evaluate_zone(self, zone_row, live: dict[str, str], now_ts: float) -> None:
        zone_id = zone_row["zone_id"]
        zone_name = zone_row["name"] or zone_id
        state = self._zone_state.setdefault(zone_id, ZoneAlertState())

        vehicle_count = _safe_int(live.get("vehicle_count"))
        max_vehicles = _safe_int(live.get("max_vehicles"), default=zone_row["max_vehicles"] or 0)
        camera_id = live.get("camera_id") or zone_row["camera_id"]
        edge_id = live.get("edge_id")
        max_dwell_observed = _safe_float(live.get("max_dwell_time_s"))
        overstay_ids = _safe_json_int_list(live.get("overstay_alert_ids"))
        dwell_limit = _safe_float(zone_row["max_dwell_time_s"], default=900.0)

        # Suppress per-zone overcrowding when this zone belongs to a group.
        # The group-level evaluation (see _evaluate_group) sums member zones
        # and fires a single alert against the group's threshold — without
        # this suppression, four cameras covering the same physical zone
        # would each fire their own overcrowding alert for the same
        # congestion event.
        grouped = zone_row["zone_group_id"] is not None
        if grouped:
            state.overcrowding_started_at = None
        elif vehicle_count > max_vehicles:
            if state.overcrowding_started_at is None:
                state.overcrowding_started_at = now_ts

            sustained_for = now_ts - state.overcrowding_started_at
            if (
                sustained_for >= self._overcrowding_sustain_s
                and now_ts - state.last_overcrowding_alert_ts >= self._overcrowding_repeat_s
            ):
                level = "critical" if vehicle_count >= max(max_vehicles + 3, int(max_vehicles * 1.25)) else "warning"
                message = (
                    f"{zone_name}: congestion sustained for "
                    f"{_fmt_dwell(sustained_for)} with {vehicle_count} vehicles "
                    f"(limit: {max_vehicles})"
                )
                await self._emit_alert(
                    ts=now_ts,
                    edge_id=edge_id,
                    zone_id=zone_id,
                    camera_id=camera_id,
                    alert_type="overcrowding",
                    level=level,
                    message=message,
                    vehicle_count=vehicle_count,
                    dwell_time_s=None,
                    track_id=None,
                )
                state.last_overcrowding_alert_ts = now_ts
        else:
            state.overcrowding_started_at = None

        # Overstay alerts are owned by the edge (vehicle_analytics.check_alerts).
        # The edge has the per-track bbox needed for cropping evidence images,
        # so it's the source of truth. Firing here too produced duplicate rows
        # ~1s apart, the second one without a bbox so it saved the full frame
        # instead of the cropped vehicle.
        del overstay_ids, dwell_limit, max_dwell_observed  # quiet linter

    async def _evaluate_all_groups(self) -> None:
        """Group-level overcrowding sweep.

        Runs alongside the per-zone sweep. Only groups with a non-NULL
        max_vehicles participate — a group with no threshold is purely
        an aggregation label and stays quiet. For each participating
        group we sum live counts across member zones from Redis and
        compare to the threshold using the same sustain/repeat timing
        as per-zone alerts.

        The sum is NOT deduplicated across cameras — a vehicle visible
        to two cameras counts twice. Cross-camera dedup needs camera
        calibration or plate OCR (deferred). For practical multi-feed
        setups where cameras cover *adjacent* areas with minimal
        overlap, the sum is a tight upper bound on actual occupancy
        and the threshold can be tuned accordingly.
        """
        groups = await self._db.fetch(
            """
            SELECT group_id, name, max_vehicles
            FROM zone_groups
            WHERE max_vehicles IS NOT NULL
            """
        )
        now_ts = time.time()
        active_group_ids = {g["group_id"] for g in groups}
        self._group_state = {
            gid: state
            for gid, state in self._group_state.items()
            if gid in active_group_ids
        }
        for group in groups:
            await self._evaluate_group(group, now_ts)

    async def _evaluate_group(self, group_row, now_ts: float) -> None:
        group_id = group_row["group_id"]
        group_name = group_row["name"] or group_id
        max_vehicles = int(group_row["max_vehicles"] or 0)
        if max_vehicles <= 0:
            return

        members = await self._db.fetch(
            "SELECT zone_id, camera_id FROM zones WHERE zone_group_id = $1 AND active = TRUE",
            group_id,
        )
        if not members:
            return

        total_count = 0
        member_feeds = 0
        # Track the member whose feed is showing the most vehicles right
        # now — that's the camera we'll grab the evidence snapshot from.
        # Operators get the busiest view of the congested group instead
        # of no image at all (camera_id=None made image_capture skip).
        busiest_count = -1
        busiest_camera_id: str | None = None
        busiest_edge_id: str | None = None
        for m in members:
            live = await self._redis.hgetall(f"vzone:{m['zone_id']}:latest")
            if not live:
                continue
            vc = _safe_int(live.get("vehicle_count"))
            total_count += vc
            member_feeds += 1
            if vc > busiest_count:
                busiest_count = vc
                busiest_camera_id = m["camera_id"] or live.get("camera_id")
                busiest_edge_id = live.get("edge_id")

        state = self._group_state.setdefault(group_id, GroupAlertState())

        if total_count > max_vehicles:
            if state.overcrowding_started_at is None:
                state.overcrowding_started_at = now_ts
            sustained_for = now_ts - state.overcrowding_started_at
            if (
                sustained_for >= self._overcrowding_sustain_s
                and now_ts - state.last_overcrowding_alert_ts >= self._overcrowding_repeat_s
            ):
                level = (
                    "critical"
                    if total_count >= max(max_vehicles + 3, int(max_vehicles * 1.25))
                    else "warning"
                )
                feed_word = "feed" if member_feeds == 1 else "feeds"
                message = (
                    f"Group {group_name}: {total_count} vehicles across "
                    f"{member_feeds} {feed_word}, sustained for "
                    f"{_fmt_dwell(sustained_for)} (limit: {max_vehicles})"
                )
                # zone_id field carries the group_id for group alerts —
                # the alert row is otherwise indistinguishable from a
                # per-zone overcrowding event, which keeps the existing
                # UI working without a schema change. The "Group <name>:"
                # prefix in message makes the scope obvious. camera_id is
                # the busiest member's camera so the evidence snapshot
                # captures the most-loaded feed; without it, image_capture
                # had no edge endpoint to hit and the alert showed up in
                # the dashboard without a thumbnail.
                await self._emit_alert(
                    ts=now_ts,
                    edge_id=busiest_edge_id,
                    zone_id=group_id,
                    camera_id=busiest_camera_id,
                    alert_type="overcrowding",
                    level=level,
                    message=message,
                    vehicle_count=total_count,
                    dwell_time_s=None,
                    track_id=None,
                )
                state.last_overcrowding_alert_ts = now_ts
        else:
            state.overcrowding_started_at = None

    async def _emit_alert(
        self,
        *,
        ts: float,
        edge_id: str | None,
        zone_id: str,
        camera_id: str | None,
        alert_type: str,
        level: str,
        message: str,
        vehicle_count: int | None,
        dwell_time_s: float | None,
        track_id: int | None,
    ) -> None:
        ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        row = await self._db.fetchrow(
            _INSERT_ALERT + " RETURNING alert_id",
            ts_dt,
            edge_id,
            zone_id,
            camera_id,
            alert_type,
            level,
            message,
            vehicle_count,
            dwell_time_s,
            track_id,
        )
        alert_id = row["alert_id"] if row else None

        # Cloud-engine alerts are always overcrowding (no per-vehicle bbox).
        image_url = None
        if alert_id is not None and camera_id:
            image_url = await capture_alert_image(
                alert_id=alert_id,
                camera_id=camera_id,
                alert_type=alert_type,
                bbox=None,
                zone_id=zone_id,
                ts=ts,
                dwell_time_s=dwell_time_s,
                track_id=track_id,
            )
            if image_url is not None:
                await self._db.execute(
                    "UPDATE vehicle_alerts SET image_url = $1 WHERE alert_id = $2",
                    image_url, alert_id,
                )

        payload = {
            "alert_id": alert_id,
            "ts": ts,
            "edge_id": edge_id,
            "zone_id": zone_id,
            "camera_id": camera_id,
            "alert_type": alert_type,
            "level": level,
            "message": message,
            "vehicle_count": vehicle_count,
            "dwell_time_s": dwell_time_s,
            "track_id": track_id,
            "image_url": image_url,
            "acknowledged": False,
        }
        await self._redis.publish(f"valert:{zone_id}", json.dumps(payload))


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_json_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    try:
        raw = json.loads(value)
        return [int(v) for v in raw]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
