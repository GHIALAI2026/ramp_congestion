"""Auto-recovery for cameras whose RTSP daemon hangs.

Watches the edge's `error_cameras` list. When a camera has been continuously
errored for more than STUCK_THRESHOLD_S seconds AND the camera itself
responds to ONVIF, sends an ONVIF SystemReboot to power-cycle the camera's
firmware. The edge's existing 30-s RTSP retry then picks it up automatically
once the camera finishes booting (~75 s observed on Tyco Illustra MFZ).

Guard rails:
  - 30-min cooldown between reboots of the same camera (REBOOT_COOLDOWN_S)
  - hard cap of 3 reboots/day per camera (DAILY_REBOOT_CAP)
  - if the ONVIF reboot call itself fails, we DON'T retry — the camera
    is either totally offline or doesn't support ONVIF, neither of which
    a reboot would fix.

State is kept in Redis (no new DB tables required):
  camwd:{cam_id}:fail_since        timestamp the camera first appeared errored
  camwd:{cam_id}:last_reboot       timestamp of last reboot attempt
  camwd:{cam_id}:count:YYYY-MM-DD  reboot count for today

Reboot events are also published to MQTT (vehicle/maintenance/reboot) so
the cloud alert log or dashboard can show "auto-reboot triggered at HH:MM".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, unquote

logger = logging.getLogger(__name__)


# Tune-ables — conservative defaults. Override via env if needed.
POLL_INTERVAL_S = 60
STUCK_THRESHOLD_S = 600       # camera must be errored 10 min before reboot
REBOOT_COOLDOWN_S = 1800      # 30-min cooldown per camera
DAILY_REBOOT_CAP = 3          # max reboots per camera per day
ONVIF_TIMEOUT_S = 10          # ONVIF call timeout
REBOOT_RECOVERY_GRACE_S = 180  # after a reboot, wait this long for the camera
                               # to come back before declaring it "still down"
BATCH_ALERT_THRESHOLD = 3      # >= this many cameras alert-worthy in one tick
                               # → send a single combined alert (network event)


class CameraWatchdog:
    def __init__(self) -> None:
        self._db = None
        self._redis = None
        self._mqtt = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    async def start(self, db, redis) -> None:
        self._db = db
        self._redis = redis
        self._connect_mqtt()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="camera-watchdog")
        logger.info("CameraWatchdog started (poll=%ds, stuck=%ds, cooldown=%ds, daily_cap=%d)",
                    POLL_INTERVAL_S, STUCK_THRESHOLD_S, REBOOT_COOLDOWN_S, DAILY_REBOOT_CAP)

    def _connect_mqtt(self) -> None:
        """Lazy MQTT publisher so camera_offline alerts flow through the same
        edge→broker→cloud alert path as overstay/overcrowding. The cloud's own
        MQTT consumer ingests them, writes the vehicle_alerts row, and
        broadcasts to the dashboard — so we reuse all of that instead of
        re-implementing DB insert + websocket broadcast here."""
        try:
            import paho.mqtt.client as mqtt
            from cloud.config import settings
            self._mqtt = mqtt.Client()
            self._mqtt.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
            self._mqtt.loop_start()
            logger.info("CameraWatchdog MQTT publisher connected")
        except Exception:
            logger.exception("CameraWatchdog: MQTT connect failed; offline alerts disabled")
            self._mqtt = None

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        if self._mqtt is not None:
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:
                pass
            self._mqtt = None

    async def _run(self) -> None:
        # First tick after a short delay so the cloud finishes startup before
        # we start poking cameras.
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=15)
            return  # stop requested before we even started
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("CameraWatchdog tick failed; continuing")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        errored = await self._get_errored_cameras()
        cameras = await self._get_camera_urls()  # {cam_id: source_url}

        now = int(time.time())

        # Clear the "fail_since" counter for cameras that have recovered, so
        # the next failure starts a fresh 10-min timer. If we'd alerted on the
        # camera being offline, publish a recovery note and clear the flag.
        for cam_id in list(cameras.keys()):
            if cam_id not in errored:
                key = f"camwd:{cam_id}:fail_since"
                if await self._redis.exists(key):
                    await self._redis.delete(key)
                alerted_key = f"camwd:{cam_id}:alerted"
                if await self._redis.exists(alerted_key):
                    await self._redis.delete(alerted_key)
                    await self._publish_camera_alert(
                        [cam_id], recovered=True,
                        message=f"Camera {cam_id} is back online.",
                    )

        # Stamp fail-start time on every newly-errored camera.
        for cam_id in errored:
            fs_key = f"camwd:{cam_id}:fail_since"
            if not await self._redis.exists(fs_key):
                await self._redis.set(fs_key, now)

        # Collect cameras that are "alert-worthy" this tick: down past the
        # threshold AND auto-recovery has been given its chance (a reboot was
        # attempted >grace ago and it's still down, OR we've exhausted the
        # daily reboot cap and it's still down). Not-yet-alerted only.
        alert_worthy: list[str] = []

        # Look for candidates to reboot.
        for cam_id in errored:
            url = cameras.get(cam_id)
            if not url:
                continue  # not in DB anymore

            fs = await self._redis.get(f"camwd:{cam_id}:fail_since")
            if not fs:
                continue
            errored_for = now - int(fs)
            if errored_for < STUCK_THRESHOLD_S:
                continue

            already_alerted = bool(await self._redis.exists(f"camwd:{cam_id}:alerted"))
            lr = await self._redis.get(f"camwd:{cam_id}:last_reboot")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            count_key = f"camwd:{cam_id}:count:{today}"
            today_count = int(await self._redis.get(count_key) or 0)

            # Alert-worthy = we've given auto-recovery its chance and the
            # camera is STILL down:
            #   * a reboot was attempted > grace ago and it's still errored, OR
            #   * the daily reboot cap is exhausted (can't try anymore)
            if not already_alerted:
                rebooted_long_ago = lr and (now - int(lr)) > REBOOT_RECOVERY_GRACE_S
                cap_exhausted = today_count >= DAILY_REBOOT_CAP
                if rebooted_long_ago or cap_exhausted:
                    alert_worthy.append(cam_id)

            # Cooldown check (don't reboot again too soon)
            if lr and (now - int(lr)) < REBOOT_COOLDOWN_S:
                continue

            # Daily cap check
            if today_count >= DAILY_REBOOT_CAP:
                logger.info("CameraWatchdog: %s hit daily reboot cap (%d); skipping reboot",
                            cam_id, DAILY_REBOOT_CAP)
                continue

            # All guards passed — attempt reboot
            ok = await self._reboot_camera(cam_id, url)
            await self._redis.set(f"camwd:{cam_id}:last_reboot", now)
            # Set 26h TTL on the daily counter so it self-expires.
            new_count = await self._redis.incr(count_key)
            await self._redis.expire(count_key, 26 * 3600)

            logger.warning(
                "CameraWatchdog: triggered ONVIF reboot for %s (errored=%ds, today=%d/%d, success=%s)",
                cam_id, errored_for, new_count, DAILY_REBOOT_CAP, ok,
            )
            await self._publish_event(cam_id, errored_for, today_count + 1, ok)

        # Fire offline alert(s) for everything that's still down after recovery
        # had its chance. Batch when many drop together (network/power event).
        if alert_worthy:
            for cam_id in alert_worthy:
                await self._redis.set(f"camwd:{cam_id}:alerted", now)
            if len(alert_worthy) >= BATCH_ALERT_THRESHOLD:
                names = ", ".join(sorted(alert_worthy))
                await self._publish_camera_alert(
                    alert_worthy, recovered=False, level="critical",
                    message=(f"{len(alert_worthy)} cameras offline after auto-recovery "
                             f"failed: {names}. Likely a network or power event — "
                             f"check the switch/uplink."),
                )
            else:
                for cam_id in alert_worthy:
                    await self._publish_camera_alert(
                        [cam_id], recovered=False, level="warning",
                        message=(f"Camera {cam_id} offline — auto-reboot did not "
                                 f"recover it. Needs a manual check."),
                    )

    # ------------------------------------------------------------------

    async def _get_errored_cameras(self) -> set[str]:
        """Read error_cameras from the most recent edge heartbeat hash(es).

        The consumer stores heartbeats as a Redis HASH at
        `vedge:{edge_id}:heartbeat`, with error_cameras a JSON-encoded field.
        We also ignore stale heartbeats (edge dead/restarting): if the last
        heartbeat is older than HEARTBEAT_STALE_S, error_cameras is
        meaningless, so we return empty and let the system-down path (out of
        scope here) handle a dead edge.
        """
        HEARTBEAT_STALE_S = 120
        try:
            keys = await self._redis.keys("vedge:*:heartbeat")
        except Exception:
            return set()
        now = time.time()
        errored: set[str] = set()
        for k in keys:
            try:
                h = await self._redis.hgetall(k)
                if not h:
                    continue
                ts = float(h.get("ts") or 0)
                if now - ts > HEARTBEAT_STALE_S:
                    continue  # stale heartbeat — edge likely down, skip
                for c in json.loads(h.get("error_cameras") or "[]"):
                    errored.add(str(c))
            except Exception:
                continue
        return errored

    async def _get_camera_urls(self) -> dict[str, str]:
        rows = await self._db.fetch(
            "SELECT camera_id, source_url FROM cameras WHERE source_url IS NOT NULL"
        )
        return {r["camera_id"]: r["source_url"] for r in rows}

    async def _reboot_camera(self, cam_id: str, source_url: str) -> bool:
        """Issue ONVIF SystemReboot. Returns True iff the camera ack'd."""
        host, user, pwd = _parse_rtsp(source_url)
        if not host or not user or not pwd:
            logger.warning("CameraWatchdog: %s — can't parse rtsp creds, skip", cam_id)
            return False

        # ONVIF is sync (zeep); run in a thread so we don't block the event loop.
        def _call():
            from onvif import ONVIFCamera
            cam = ONVIFCamera(host, 80, user, pwd)
            dev = cam.create_devicemgmt_service()
            return dev.SystemReboot()

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_call), timeout=ONVIF_TIMEOUT_S
            )
            logger.info("CameraWatchdog: %s reboot ack: %s", cam_id, result)
            return True
        except Exception as e:
            logger.warning(
                "CameraWatchdog: %s reboot failed (%s) — camera unreachable or no ONVIF",
                cam_id, type(e).__name__,
            )
            return False

    async def _publish_camera_alert(
        self, cam_ids: list[str], *, recovered: bool,
        message: str, level: str = "warning",
    ) -> None:
        """Publish a camera_offline (or recovery) alert onto the same MQTT
        alert topic the edge uses, so the cloud's MQTT consumer ingests it,
        writes the vehicle_alerts row, and broadcasts to the dashboard.

        zone_id is required NOT NULL on vehicle_alerts; for a per-camera
        alert we put the camera_id there so the chip reads sensibly, and for
        a batch we use a synthetic 'system' scope. camera_id slot carries
        the first/only camera.
        """
        if self._mqtt is None:
            logger.warning("CameraWatchdog: MQTT publisher unavailable; can't send alert")
            return
        from cloud.config import settings
        edge_id = settings.edge_id
        primary = cam_ids[0] if len(cam_ids) == 1 else "multiple"
        zone_slot = cam_ids[0] if len(cam_ids) == 1 else "system"
        payload = {
            "v": 1,
            "edge_id": edge_id,
            "zone_id": zone_slot,
            "camera_id": primary,
            "ts": time.time(),
            "alert_type": "camera_recovered" if recovered else "camera_offline",
            "level": "info" if recovered else level,
            "message": message,
        }
        topic = f"{settings.mqtt_topic_prefix}/edge/{edge_id}/alert/" + payload["alert_type"]
        try:
            # paho publish is sync + thread-safe; fine to call from the loop.
            self._mqtt.publish(topic, json.dumps(payload), qos=1)
            logger.warning("CameraWatchdog: published %s for %s",
                           payload["alert_type"], cam_ids)
        except Exception:
            logger.exception("CameraWatchdog: failed to publish camera alert")

    async def _publish_event(
        self, cam_id: str, errored_for: int, today_count: int, success: bool,
    ) -> None:
        # Publish to a Redis pubsub channel so the cloud websocket gateway can
        # forward it to the dashboard if it wants to surface auto-reboots in
        # the alert drawer. Keeping it lightweight — no DB write.
        try:
            await self._redis.publish(
                "vehicle/maintenance/reboot",
                json.dumps({
                    "camera_id": cam_id,
                    "errored_for_s": errored_for,
                    "today_count": today_count,
                    "daily_cap": DAILY_REBOOT_CAP,
                    "success": success,
                    "ts": int(time.time()),
                }),
            )
        except Exception:
            logger.debug("CameraWatchdog: pubsub publish failed (non-fatal)")


def _parse_rtsp(url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Pull host, user, password out of an rtsp://user:pass@host[:port]/path URL.

    Credentials in the DB are URL-encoded (e.g. Ap0%28%402024 → Ap0(@2024).
    """
    try:
        p = urlparse(url)
        host = p.hostname
        user = unquote(p.username) if p.username else None
        pwd = unquote(p.password) if p.password else None
        return host, user, pwd
    except Exception:
        return None, None, None
