"""MQTT publisher: sends vehicle zone metrics, alerts, and heartbeats to the local broker."""

from __future__ import annotations

import json
import logging
import os
import time

import paho.mqtt.client as mqtt

from edge_agent import config as cfg
from edge_agent.schemas import EdgeHeartbeat, VehicleZoneMetrics, VehicleAlert

logger = logging.getLogger(__name__)

_LOG_SUPPRESS_INTERVAL_S = 30.0

class MQTTPublisher:
    def __init__(self):
        unique_id = f"vehicle-edge-pub-{cfg.EDGE_ID}-{os.getpid()}"
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=unique_id,
        )
        self._client.max_inflight_messages_set(cfg.MQTT_MAX_INFLIGHT)
        self._client.max_queued_messages_set(cfg.MQTT_MAX_QUEUED)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._connected = False
        self._last_connect_log = 0.0
        self._last_disconnect_log = 0.0

    def connect(self) -> None:
        self._client.connect(cfg.MQTT_HOST, cfg.MQTT_PORT, keepalive=30)
        self._client.loop_start()
        for _ in range(20):
            if self._connected:
                break
            time.sleep(0.1)

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish_metrics(self, metrics: VehicleZoneMetrics) -> None:
        if not self._connected:
            return
        topic = f"{cfg.MQTT_TOPIC_PREFIX}/edge/{cfg.EDGE_ID}/zone/{metrics.zone_id}"
        payload = json.dumps(metrics.model_dump(mode='json'))
        self._client.publish(topic, payload, qos=cfg.MQTT_TELEMETRY_QOS)

    def publish_alert(self, alert: VehicleAlert) -> None:
        if not self._connected:
            return
        topic = f"{cfg.MQTT_TOPIC_PREFIX}/edge/{cfg.EDGE_ID}/alert/{alert.alert_type}"
        payload = json.dumps(alert.model_dump(mode='json'))
        self._client.publish(topic, payload, qos=cfg.MQTT_ALERT_QOS)

    def publish_heartbeat(self, hb: EdgeHeartbeat) -> None:
        if not self._connected:
            return
        topic = f"{cfg.MQTT_TOPIC_PREFIX}/edge/{cfg.EDGE_ID}/heartbeat"
        payload = json.dumps(hb.model_dump(mode='json'))
        self._client.publish(topic, payload, qos=cfg.MQTT_TELEMETRY_QOS, retain=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self._connected = True
            now = time.monotonic()
            if now - self._last_connect_log >= _LOG_SUPPRESS_INTERVAL_S:
                logger.info("MQTT publisher connected to %s:%d", cfg.MQTT_HOST, cfg.MQTT_PORT)
                self._last_connect_log = now
        else:
            logger.error("MQTT publisher connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = False
        if reason_code != 0:
            now = time.monotonic()
            if now - self._last_disconnect_log >= _LOG_SUPPRESS_INTERVAL_S:
                logger.warning(
                    "MQTT publisher disconnected (rc=%s), reconnecting...",
                    reason_code,
                )
                self._last_disconnect_log = now
