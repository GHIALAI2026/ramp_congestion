"""MQTT listener: receives zone config and camera assignment commands from the cloud."""

from __future__ import annotations

import json
import logging
import os

import paho.mqtt.client as mqtt

from edge_agent import config as cfg
from edge_agent.schemas import ZoneConfig
from rtsp_utils import normalize_rtsp_url

logger = logging.getLogger(__name__)


class MQTTListener:
    """
    Subscribes to control topics:
      vehicle/control/{edge_id}/config   — zone config update
      vehicle/control/{edge_id}/assign   — camera add/remove
    """

    def __init__(self, camera_manager):
        self._camera_manager = camera_manager
        # PID suffix prevents the broker from kicking us off when a stale
        # session for the prior process is still hanging around — without
        # this the new listener and the zombie ping-pong each other off
        # and on_connect never settles long enough to subscribe.
        client_id = f"vehicle-edge-listener-{cfg.EDGE_ID}-{os.getpid()}"
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client_id = client_id

    def start(self) -> None:
        try:
            self._client.connect(cfg.MQTT_HOST, cfg.MQTT_PORT, keepalive=30)
        except Exception:
            # Log + re-raise so the edge doesn't silently come up without
            # a control plane. Auto-reconnect will retry once the loop runs.
            logger.exception("MQTT listener initial connect to %s:%d failed",
                             cfg.MQTT_HOST, cfg.MQTT_PORT)
            raise
        # paho-mqtt 2.x drives its socket via select.select() which is
        # hard-capped at FD_SETSIZE=1024 on Linux/CPython. If we land on
        # a high FD because the listener was created after the rest of
        # the edge had already burned through ~2000 FDs (SHM slots,
        # worker pipes, RTSP decoders), on_connect silently never fires.
        # Be loud about it so a future regression doesn't ship as
        # "control plane is dead but nothing logs why."
        sock = self._client.socket()
        if sock is not None and sock.fileno() >= 1024:
            logger.error(
                "MQTT listener socket fd=%d exceeds select() FD_SETSIZE=1024; "
                "on_connect will never fire. Move listener.start() earlier "
                "in main.py before SHM/worker allocation.",
                sock.fileno(),
            )
        self._client.loop_start()
        logger.info("MQTT listener started (client_id=%s, fd=%s)",
                    self._client_id, sock.fileno() if sock else "n/a")

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # Always log entry so a non-zero reason_code is visible instead of
        # disappearing into the void.
        if reason_code == 0:
            prefix = cfg.MQTT_TOPIC_PREFIX
            client.subscribe(f"{prefix}/control/{cfg.EDGE_ID}/config", qos=1)
            client.subscribe(f"{prefix}/control/{cfg.EDGE_ID}/assign", qos=1)
            logger.info(
                "MQTT listener subscribed to %s/control/%s/{config,assign}",
                prefix, cfg.EDGE_ID,
            )
        else:
            logger.error("MQTT listener connect rejected: reason_code=%s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        # paho's loop thread will auto-reconnect (reconnect_delay_set above);
        # we just want operators to see the drop in the log instead of
        # silently losing subscriptions.
        if reason_code != 0:
            logger.warning("MQTT listener disconnected (reason_code=%s); will reconnect",
                           reason_code)

    def _on_message(self, client, userdata, message):
        topic = message.topic
        try:
            data = json.loads(message.payload.decode())
        except json.JSONDecodeError:
            logger.warning("Bad JSON on %s", topic)
            return

        if topic.endswith("/config"):
            self._handle_zone_config(data)
        elif topic.endswith("/assign"):
            self._handle_assign(data)

    def _handle_zone_config(self, data: dict) -> None:
        try:
            zone_cfg = ZoneConfig(**data)
            self._camera_manager.update_zone(zone_cfg)
            logger.info("Zone config updated: %s", zone_cfg.zone_id)
        except Exception as e:
            import traceback
            logger.error("Failed to process zone config: %s\n%s", e, traceback.format_exc())

    def _handle_assign(self, data: dict) -> None:
        action = data.get("action")
        camera_id = data.get("camera_id")
        if action == "add":
            source_url = normalize_rtsp_url(data.get("source_url", ""))
            zones_raw = data.get("zones", [])
            zones = [ZoneConfig(**z) for z in zones_raw]
            self._camera_manager.add_camera(camera_id, source_url, zones)
        elif action == "sync":
            source_url = normalize_rtsp_url(data.get("source_url", ""))
            zones_raw = data.get("zones", [])
            zones = [ZoneConfig(**z) for z in zones_raw]
            self._camera_manager.sync_camera(camera_id, source_url, zones)
        elif action == "remove":
            self._camera_manager.remove_camera(camera_id)
