"""WebSocket gateway for real-time vehicle zone dashboard updates.

Clients connect to /ws/dashboard, subscribe to topics, and receive
streaming vehicle zone metric updates and alerts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from cloud.models.db import get_redis

logger = logging.getLogger(__name__)


@dataclass
class DashboardClient:
    ws: WebSocket
    subscriptions: set[str] = field(default_factory=set)

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other


_clients: set[DashboardClient] = set()
_overview_zone_cache: dict[str, dict[str, Any]] = {}


async def _safe_send(client: DashboardClient, payload: dict[str, Any]) -> bool:
    try:
        if client.ws.application_state == WebSocketState.CONNECTED:
            await client.ws.send_json(payload)
            return True
    except Exception:
        pass
    return False


async def _pubsub_listener() -> None:
    """Subscribe to Redis channels and fan-out to connected dashboard clients."""
    redis = await get_redis()

    while True:
        try:
            pubsub = redis.pubsub()
            await pubsub.psubscribe("vmetrics:*", "valert:*")
            logger.info("WebSocket gateway subscribed to Redis pub/sub")

            async for message in pubsub.listen():
                if message["type"] not in ("pmessage",):
                    continue

                channel: str = message["channel"]
                try:
                    data = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                if channel.startswith("vmetrics:"):
                    zone_id = channel.split(":", 1)[1]
                    _overview_zone_cache[zone_id] = data
                    await _dispatch_metric(zone_id, data)

                elif channel.startswith("valert:"):
                    zone_id = channel.split(":", 1)[1]
                    await _dispatch_alert(zone_id, data)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Pub/sub listener error, reconnecting in 2s")
            await asyncio.sleep(2)


async def _dispatch_metric(zone_id: str, data: dict[str, Any]) -> None:
    dead: list[DashboardClient] = []
    for client in list(_clients):
        try:
            if f"zone:{zone_id}" in client.subscriptions:
                ok = await _safe_send(client, {
                    "type": "zone_metric",
                    "zone_id": zone_id,
                    "data": data,
                })
                if not ok:
                    dead.append(client)
        except Exception:
            dead.append(client)
    for c in dead:
        _clients.discard(c)


async def _dispatch_alert(zone_id: str, data: dict[str, Any]) -> None:
    dead: list[DashboardClient] = []
    payload = {"type": "alert", "zone_id": zone_id, "data": data}
    for client in list(_clients):
        ok = await _safe_send(client, payload)
        if not ok:
            dead.append(client)
    for c in dead:
        _clients.discard(c)


async def _overview_timer() -> None:
    """Every 1 second, broadcast overview to subscribers."""
    while True:
        try:
            await asyncio.sleep(1)
            subscribers = [c for c in _clients if "overview" in c.subscriptions]
            if not subscribers:
                continue

            # Aggregate overview
            total_vehicles = 0
            total_overstay = 0
            total_overcrowding = 0
            zone_count = len(_overview_zone_cache)
            for zid, data in _overview_zone_cache.items():
                total_vehicles += data.get("vehicle_count", 0)
                total_overstay += data.get("overstay_count", 0)
                if data.get("overcrowding_alert"):
                    total_overcrowding += 1

            payload = {
                "type": "overview",
                "data": {
                    "total_vehicles": total_vehicles,
                    "total_zones": zone_count,
                    "overcrowding_zones": total_overcrowding,
                    "total_overstay": total_overstay,
                },
            }

            dead = []
            for client in subscribers:
                ok = await _safe_send(client, payload)
                if not ok:
                    dead.append(client)
            for c in dead:
                _clients.discard(c)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Overview timer error")
            await asyncio.sleep(1)


async def _handle_client(ws: WebSocket) -> None:
    await ws.accept()
    client = DashboardClient(ws=ws)
    _clients.add(client)
    logger.info("Dashboard client connected (%d total)", len(_clients))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _safe_send(client, {"type": "error", "message": "Invalid JSON"})
                continue

            action = msg.get("action")
            topics = msg.get("topics", [])

            if action == "subscribe":
                client.subscriptions.update(topics)
                await _safe_send(client, {
                    "type": "subscribed",
                    "topics": sorted(client.subscriptions),
                })
            elif action == "unsubscribe":
                client.subscriptions -= set(topics)
                await _safe_send(client, {
                    "type": "unsubscribed",
                    "topics": sorted(client.subscriptions),
                })
            else:
                await _safe_send(client, {
                    "type": "error",
                    "message": f"Unknown action: {action}",
                })

    except WebSocketDisconnect:
        logger.info("Dashboard client disconnected")
    except Exception:
        logger.exception("WebSocket handler error")
    finally:
        _clients.discard(client)


_background_tasks: list[asyncio.Task] = []


def setup_websocket(app: FastAPI) -> None:
    app.add_api_websocket_route("/ws/dashboard", _handle_client)


async def start_ws_tasks() -> None:
    _background_tasks.append(asyncio.create_task(_pubsub_listener()))
    _background_tasks.append(asyncio.create_task(_overview_timer()))
    logger.info("WebSocket gateway background tasks started")


async def stop_ws_tasks() -> None:
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()
