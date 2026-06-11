"""MQTT consumer that ingests vehicle zone telemetry into Redis and TimescaleDB."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from datetime import datetime, timezone
from typing import Any

import asyncpg
import paho.mqtt.client as mqtt
import redis.asyncio as aioredis

from cloud.config import settings
from cloud.models.db import get_db, get_redis
from cloud.models.schemas import VehicleAlertMsg, EdgeHeartbeatMsg, VehicleZoneMetricsMsg
from cloud.modules.alerts.image_capture import capture_alert_image

logger = logging.getLogger(__name__)

_INSERT_ZONE_METRICS = """
INSERT INTO vehicle_zone_metrics (
    ts, edge_id, zone_id, camera_id,
    vehicle_count, vehicle_count_by_type, occupancy_pct,
    overstay_count, avg_dwell_time_s, max_dwell_time_s,
    total_entered, total_exited,
    overcrowding_alert, active_track_count,
    inf_fps, inf_ms
) VALUES (
    $1, $2, $3, $4,
    $5, $6, $7,
    $8, $9, $10,
    $11, $12,
    $13, $14,
    $15, $16
)
"""

_INSERT_ALERT = """
INSERT INTO vehicle_alerts (
    ts, edge_id, zone_id, camera_id,
    alert_type, level, message,
    vehicle_count, dwell_time_s, track_id
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""


class _ZoneMsg:
    __slots__ = ("msg", "raw_json")
    def __init__(self, msg: VehicleZoneMetricsMsg, raw_json: str) -> None:
        self.msg = msg
        self.raw_json = raw_json


class _HeartbeatMsg:
    __slots__ = ("msg",)
    def __init__(self, msg: EdgeHeartbeatMsg) -> None:
        self.msg = msg


class _AlertMsgWrapper:
    __slots__ = ("msg", "raw_json")
    def __init__(self, msg: VehicleAlertMsg, raw_json: str) -> None:
        self.msg = msg
        self.raw_json = raw_json


class MQTTConsumer:
    """Consumes MQTT messages from edge devices and persists them."""

    def __init__(self) -> None:
        self._mqtt: mqtt.Client | None = None
        self._db: asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
        self._queue: queue.Queue = queue.Queue(
            maxsize=max(1, int(settings.mqtt_ingest_queue_max))
        )
        self._batch: list[tuple[Any, ...]] = []
        self._dropped_queue_items = 0
        self._last_queue_drop_log = 0.0
        self._running = False
        self._dispatcher_task: asyncio.Task | None = None
        self._flusher_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._db = await get_db()
        self._redis = await get_redis()
        self._running = True

        self._mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"vehicle-cloud-consumer-{int(time.time())}",
        )
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message
        self._mqtt.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
        self._mqtt.loop_start()

        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())
        self._flusher_task = asyncio.create_task(self._flush_loop())

        logger.info("MQTTConsumer started (broker=%s:%d)", settings.mqtt_host, settings.mqtt_port)

    async def stop(self) -> None:
        self._running = False
        if self._mqtt is not None:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            self._mqtt = None

        for task in (self._dispatcher_task, self._flusher_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._flush_batch()
        logger.info("MQTTConsumer stopped")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        prefix = settings.mqtt_topic_prefix
        if reason_code == 0:
            logger.info("MQTT connected, subscribing to topics")
            client.subscribe(f"{prefix}/edge/+/zone/+", qos=1)
            client.subscribe(f"{prefix}/edge/+/heartbeat", qos=1)
            client.subscribe(f"{prefix}/edge/+/alert/+", qos=1)
        else:
            logger.error("MQTT connection failed: %s", reason_code)

    def _on_message(self, client, userdata, message):
        topic = message.topic
        payload = message.payload.decode("utf-8", errors="replace")
        parts = topic.split("/")

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON on topic %s", topic)
            return

        try:
            # vehicle/edge/{edge_id}/zone/{zone_id}
            if len(parts) == 5 and parts[3] == "zone":
                msg = VehicleZoneMetricsMsg(**data)
                self._enqueue(_ZoneMsg(msg, payload))
            # vehicle/edge/{edge_id}/heartbeat
            elif len(parts) == 4 and parts[3] == "heartbeat":
                msg_hb = EdgeHeartbeatMsg(**data)
                self._enqueue(_HeartbeatMsg(msg_hb))
            # vehicle/edge/{edge_id}/alert/{alert_type}
            elif len(parts) == 5 and parts[3] == "alert":
                msg_alert = VehicleAlertMsg(**data)
                self._enqueue(_AlertMsgWrapper(msg_alert, payload))
        except Exception:
            logger.exception("Failed to parse message on %s", topic)

    def _enqueue(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass

        self._dropped_queue_items += 1
        now = time.time()
        if now - self._last_queue_drop_log >= 10.0:
            self._last_queue_drop_log = now
            logger.warning(
                "MQTT ingest queue full; dropped %d stale message(s)",
                self._dropped_queue_items,
            )

        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._dropped_queue_items += 1

    async def _dispatcher_loop(self) -> None:
        while self._running:
            try:
                processed = 0
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break

                    if isinstance(item, _ZoneMsg):
                        await self._handle_zone_metric(item)
                    elif isinstance(item, _HeartbeatMsg):
                        await self._handle_heartbeat(item)
                    elif isinstance(item, _AlertMsgWrapper):
                        await self._handle_alert(item)

                    processed += 1

                if processed == 0:
                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dispatcher loop error")
                await asyncio.sleep(1)

    async def _handle_zone_metric(self, item: _ZoneMsg) -> None:
        msg = item.msg
        assert self._redis is not None

        hash_key = f"vzone:{msg.zone_id}:latest"
        mapping: dict[str, str] = {
            "edge_id": msg.edge_id,
            "zone_id": msg.zone_id,
            "camera_id": msg.camera_id,
            "ts": str(msg.ts),
            "vehicle_count": str(msg.vehicle_count),
            "vehicle_count_by_type": json.dumps(msg.vehicle_count_by_type),
            "max_vehicles": str(msg.max_vehicles),
            "occupancy_pct": str(msg.occupancy_pct),
            "overstay_count": str(msg.overstay_count),
            "avg_dwell_time_s": str(msg.avg_dwell_time_s),
            "max_dwell_time_s": str(msg.max_dwell_time_s),
            "total_entered": str(msg.total_entered),
            "total_exited": str(msg.total_exited),
            "overcrowding_alert": str(msg.overcrowding_alert),
            "overstay_alert_ids": json.dumps(msg.overstay_alert_ids),
            "active_track_count": str(msg.active_track_count),
            "inf_fps": str(msg.inf_fps),
            "inf_ms": str(msg.inf_ms),
        }

        try:
            pipe = self._redis.pipeline()
            pipe.hset(hash_key, mapping=mapping)
            pipe.publish(f"vmetrics:{msg.zone_id}", item.raw_json)
            await pipe.execute()
        except Exception:
            logger.exception("Redis error for zone metric %s", msg.zone_id)

        ts_dt = datetime.fromtimestamp(msg.ts, tz=timezone.utc)
        self._batch.append((
            ts_dt, msg.edge_id, msg.zone_id, msg.camera_id,
            msg.vehicle_count, json.dumps(msg.vehicle_count_by_type),
            msg.occupancy_pct, msg.overstay_count,
            msg.avg_dwell_time_s, msg.max_dwell_time_s,
            msg.total_entered, msg.total_exited,
            msg.overcrowding_alert, msg.active_track_count,
            msg.inf_fps, msg.inf_ms,
        ))

    async def _handle_heartbeat(self, item: _HeartbeatMsg) -> None:
        msg = item.msg
        assert self._redis is not None

        hash_key = f"vedge:{msg.edge_id}:heartbeat"
        mapping: dict[str, str] = {
            "edge_id": msg.edge_id,
            "ts": str(msg.ts),
            "uptime_s": str(msg.uptime_s),
            "cameras_active": str(msg.cameras_active),
            "cameras_assigned": str(msg.cameras_assigned),
            "cameras_errored": str(msg.cameras_errored),
            "zones_active": str(msg.zones_active),
            "cpu_pct": str(msg.cpu_pct),
            "mem_pct": str(msg.mem_pct),
            "error_cameras": json.dumps(msg.error_cameras),
        }
        if msg.gpu_pct is not None:
            mapping["gpu_pct"] = str(msg.gpu_pct)
        if msg.gpu_mem_pct is not None:
            mapping["gpu_mem_pct"] = str(msg.gpu_mem_pct)
        if msg.gpu_name is not None:
            mapping["gpu_name"] = msg.gpu_name
        if msg.gpu_note is not None:
            mapping["gpu_note"] = msg.gpu_note
        if msg.gpu_temp_c is not None:
            mapping["gpu_temp_c"] = str(msg.gpu_temp_c)
        if msg.gpu_mem_used_mb is not None:
            mapping["gpu_mem_used_mb"] = str(msg.gpu_mem_used_mb)
        if msg.gpu_mem_total_mb is not None:
            mapping["gpu_mem_total_mb"] = str(msg.gpu_mem_total_mb)
        if msg.gpu_active_cores is not None:
            mapping["gpu_active_cores"] = str(msg.gpu_active_cores)
        if msg.gpu_total_cores is not None:
            mapping["gpu_total_cores"] = str(msg.gpu_total_cores)
        if msg.igpu_name is not None:
            mapping["igpu_name"] = msg.igpu_name
        if msg.igpu_pct is not None:
            mapping["igpu_pct"] = str(msg.igpu_pct)
        if msg.igpu_video_pct is not None:
            mapping["igpu_video_pct"] = str(msg.igpu_video_pct)
        if msg.igpu_video_enhance_pct is not None:
            mapping["igpu_video_enhance_pct"] = str(msg.igpu_video_enhance_pct)
        if msg.igpu_note is not None:
            mapping["igpu_note"] = msg.igpu_note
        if msg.inference_fps is not None:
            mapping["inference_fps"] = str(msg.inference_fps)
        if msg.inference_ms is not None:
            mapping["inference_ms"] = str(msg.inference_ms)

        try:
            await self._redis.hset(hash_key, mapping=mapping)
        except Exception:
            logger.exception("Redis error for heartbeat %s", msg.edge_id)

    async def _handle_alert(self, item: _AlertMsgWrapper) -> None:
        msg = item.msg
        assert self._redis is not None
        assert self._db is not None

        ts_dt = datetime.fromtimestamp(msg.ts, tz=timezone.utc)

        # The edge emits overstay alerts on an escalating milestone ladder
        # (15m/30m/60m/hourly by default), one message per milestone, and is
        # itself idempotent (it persists the last-fired milestone to Redis so a
        # restart doesn't replay one). Each distinct milestone is therefore a
        # distinct DB row — that's the escalation history the operator sees.
        # This lookup exists only to absorb a *near-immediate* duplicate of the
        # same milestone (e.g. an edge re-send right after a restart when its
        # Redis state was unavailable): we collapse it into the existing row if
        # one for the same (zone_id, track_id) appeared within the (small) dedup
        # window. The window MUST stay below the smallest milestone gap (= the
        # zone threshold, 15 min by default) so genuine milestones are never
        # merged; longer gaps are treated as fresh rows (also handles OC-SORT
        # track-id reuse across visits). Acked rows stop accepting updates
        # entirely — the operator handled that specific alert.
        existing_alert_id: int | None = None
        existing_acked = False
        existing_image_url: str | None = None
        if msg.alert_type == "overstay" and msg.track_id is not None:
            try:
                existing = await self._db.fetchrow(
                    """
                    SELECT alert_id, acknowledged, image_url
                    FROM vehicle_alerts
                    WHERE zone_id = $1 AND track_id = $2
                      AND alert_type = 'overstay'
                      AND ts > NOW() - make_interval(secs => $3)
                    ORDER BY ts DESC LIMIT 1
                    """,
                    msg.zone_id, msg.track_id,
                    settings.overstay_alert_dedup_window_s,
                )
                if existing:
                    existing_alert_id = existing["alert_id"]
                    existing_acked = bool(existing["acknowledged"])
                    existing_image_url = existing["image_url"]
            except Exception:
                logger.exception(
                    "DB lookup for existing overstay alert failed (zone=%s tid=%s)",
                    msg.zone_id, msg.track_id,
                )

        if existing_alert_id is not None and existing_acked:
            # Operator already handled this vehicle. Drop the heartbeat
            # silently — no DB write, no broadcast, no toast.
            return

        is_update = existing_alert_id is not None
        alert_id: int | None = None
        image_url: str | None = None

        try:
            if is_update:
                await self._db.execute(
                    """
                    UPDATE vehicle_alerts
                       SET ts = $1, level = $2, message = $3, dwell_time_s = $4
                     WHERE alert_id = $5
                    """,
                    ts_dt, msg.level, msg.message, msg.dwell_time_s, existing_alert_id,
                )
                alert_id = existing_alert_id
                image_url = existing_image_url
            else:
                row = await self._db.fetchrow(
                    _INSERT_ALERT + " RETURNING alert_id",
                    ts_dt, msg.edge_id, msg.zone_id, msg.camera_id,
                    msg.alert_type, msg.level, msg.message,
                    msg.vehicle_count, msg.dwell_time_s, msg.track_id,
                )
                alert_id = row["alert_id"] if row else None
        except Exception:
            logger.exception("DB upsert error for alert %s/%s", msg.zone_id, msg.alert_type)

        # Image evidence: capture on first insert AND re-capture on overstay
        # updates. The row's dwell_time_s and the dwell chip painted on the
        # offender bbox must stay in sync — leaving the first-violation
        # image attached makes every overstay >15 min look like a 15-min
        # overstay. The on-disk path is always /static/alert_images/{id}.jpg;
        # re-capture overwrites it. A ?v=<dwell> query param appended to
        # image_url forces the dashboard to re-fetch instead of using its
        # cached copy. Failure of a re-capture leaves the previous image
        # intact (image_url stays at existing_image_url).
        if alert_id is not None and msg.camera_id:
            new_image_url = await capture_alert_image(
                alert_id=alert_id,
                camera_id=msg.camera_id,
                alert_type=msg.alert_type,
                bbox=msg.bbox,
                zone_id=msg.zone_id,
                ts=msg.ts,
                dwell_time_s=msg.dwell_time_s,
                track_id=msg.track_id,
                frame_w=msg.frame_w,
                frame_h=msg.frame_h,
                zone_poly=msg.zone_poly,
            )
            if new_image_url is not None:
                v = (
                    int(msg.dwell_time_s) if msg.dwell_time_s is not None
                    else int(msg.ts)
                )
                image_url = f"{new_image_url}?v={v}"
                try:
                    await self._db.execute(
                        "UPDATE vehicle_alerts SET image_url = $1 WHERE alert_id = $2",
                        image_url, alert_id,
                    )
                except Exception:
                    logger.exception("DB update of image_url failed for alert %s", alert_id)

        # Re-pack the broadcast. Updates carry the SAME alert_id so the
        # dashboard's mergeAlertItems collapses them into the existing row
        # in-place instead of prepending a new entry.
        try:
            broadcast = msg.model_dump()
            broadcast["alert_id"] = alert_id
            broadcast["image_url"] = image_url
            broadcast["acknowledged"] = False
            await self._redis.publish(f"valert:{msg.zone_id}", json.dumps(broadcast))
        except Exception:
            logger.exception("Redis publish error for alert %s", msg.zone_id)

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(1.0)
                await self._flush_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Flush loop error")
                await asyncio.sleep(1)

    async def _flush_batch(self) -> None:
        if not self._batch:
            return
        assert self._db is not None
        rows = self._batch
        self._batch = []
        try:
            await self._db.executemany(_INSERT_ZONE_METRICS, rows)
            logger.debug("Flushed %d vehicle zone metric rows to TimescaleDB", len(rows))
        except Exception:
            logger.exception("Failed to flush %d vehicle zone metric rows", len(rows))
            self._batch = rows + self._batch
