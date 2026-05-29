"""Edge command publisher: sends zone configs and assignments to edge devices."""

from __future__ import annotations

import json
import logging
import time

import paho.mqtt.client as mqtt

from cloud.config import settings

logger = logging.getLogger(__name__)


class EdgePublisher:
    def __init__(self):
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"vehicle-cloud-publisher-{int(time.time())}",
        )
        self._connected = False

    async def start(self) -> None:
        self._client.on_connect = self._on_connect
        self._client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
        self._client.loop_start()
        logger.info("Edge publisher started")

    async def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish_zone_config(self, edge_id: str, zone_data: dict) -> None:
        if not self._connected:
            return
        topic = f"{settings.mqtt_topic_prefix}/control/{edge_id}/config"
        self._client.publish(topic, json.dumps(zone_data), qos=1)

    def publish_assign(self, edge_id: str, assign_data: dict) -> None:
        if not self._connected:
            return
        topic = f"{settings.mqtt_topic_prefix}/control/{edge_id}/assign"
        self._client.publish(topic, json.dumps(assign_data), qos=1)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = reason_code == 0
        if self._connected:
            logger.info("Edge publisher MQTT connected")
