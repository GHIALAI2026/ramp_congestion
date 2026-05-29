"""
Config store: fetches camera assignments and zone configs from the cloud API on startup.
Caches to disk for fast restarts.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from edge_agent import config as cfg
from edge_agent.schemas import CameraConfig, ZoneConfig
from rtsp_utils import normalize_rtsp_url

logger = logging.getLogger(__name__)

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "config_cache.json")


class ConfigStore:
    def __init__(self):
        self._cameras: list[CameraConfig] = []

    def load(self) -> list[CameraConfig]:
        """
        Fetch cameras and zones from cloud API.
        Falls back to cached config if cloud is unreachable.
        """
        try:
            cameras = self._fetch_from_api()
            self._cameras = cameras
            self._save_cache(cameras)
            logger.info("Loaded %d cameras from cloud API", len(cameras))
        except Exception as e:
            logger.warning("Could not reach cloud API (%s), loading from cache", e)
            cameras = self._load_cache()
            self._cameras = cameras
            logger.info("Loaded %d cameras from cache", len(cameras))

        return self._cameras

    def get_cameras(self) -> list[CameraConfig]:
        return self._cameras

    def _fetch_from_api(self) -> list[CameraConfig]:
        base = cfg.CLOUD_API_URL
        with httpx.Client(timeout=5.0) as client:
            cam_resp = client.get(f"{base}/api/cameras")
            cam_resp.raise_for_status()
            all_cameras = cam_resp.json()

            zone_resp = client.get(f"{base}/api/zones")
            zone_resp.raise_for_status()
            all_zones = zone_resp.json()

        zones_by_camera: dict[str, list[ZoneConfig]] = {}
        for z in all_zones:
            cam_id = z.get("camera_id")
            if not cam_id:
                continue
            zone_poly = z.get("zone_poly") or []
            if not zone_poly:
                continue
            try:
                zone_cfg = ZoneConfig(
                    zone_id=z["zone_id"],
                    camera_id=cam_id,
                    name=z.get("name", z["zone_id"]),
                    zone_poly=zone_poly,
                    max_vehicles=z.get("max_vehicles", 20),
                    max_dwell_time_s=z.get("max_dwell_time_s", 900.0),
                )
                zones_by_camera.setdefault(cam_id, []).append(zone_cfg)
            except Exception:
                logger.exception("Skipping invalid zone %s", z.get("zone_id"))

        cameras: list[CameraConfig] = []
        for c in all_cameras:
            if c.get("assigned_edge") != cfg.EDGE_ID:
                continue
            cam_id = c["camera_id"]
            source_url = normalize_rtsp_url(c.get("source_url"))
            cameras.append(CameraConfig(
                camera_id=cam_id,
                name=c.get("name"),
                source_url=source_url,
                assigned_edge=c["assigned_edge"],
                zones=zones_by_camera.get(cam_id, []),
            ))

        return cameras

    def _save_cache(self, cameras: list[CameraConfig]) -> None:
        data = [c.model_dump() for c in cameras]
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _CACHE_PATH)

    def _load_cache(self) -> list[CameraConfig]:
        if not os.path.exists(_CACHE_PATH):
            return []
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        normalized = []
        for camera in data:
            camera["source_url"] = normalize_rtsp_url(camera.get("source_url"))
            normalized.append(CameraConfig(**camera))
        return normalized
