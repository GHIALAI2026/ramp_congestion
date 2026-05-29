"""Vehicle zone analytics: computes per-zone vehicle metrics from tracked detections.

Metrics computed per zone:
  - Vehicle count (total + by type)
  - Zone occupancy percentage
  - Per-vehicle dwell time tracking
  - Overstay detection
  - Overcrowding detection
  - Entry/exit counting
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

import cv2
import numpy as np

from edge_agent.schemas import ZoneConfig, VehicleZoneMetrics, VehicleAlert
from edge_agent import config as cfg

_log = logging.getLogger(__name__)


def _fmt_dwell(secs: float) -> str:
    """Format a duration as `Hh Mm Ss` / `Mm Ss` / `Ss` for human-readable alerts."""
    s = max(0, int(secs))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m {s % 60}s"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


class VehicleZoneAnalytics:
    """
    One instance per zone.
    Tracks per-vehicle dwell times and computes zone occupancy metrics at 1 Hz.
    """

    def __init__(
        self,
        zone_config: ZoneConfig,
        edge_id: str,
        redis_client: Optional[Any] = None,
    ):
        self._cfg = zone_config
        self._edge_id = edge_id
        self._redis = redis_client
        self._redis_warned = False  # log Redis errors at most once per instance
        self._zone_poly_canvas: Optional[np.ndarray] = None
        # O(1) point-in-polygon test cache: keyed by (frame_w, frame_h)
        self._mask_cache: dict[tuple[int, int], np.ndarray] = {}
        self._update_polygons(zone_config)

        # Per-track state
        self._zone_entry: dict[int, float] = {}      # track_id -> entry_time
        self._zone_class: dict[int, int] = {}        # track_id -> class_id
        self._zone_bbox: dict[int, tuple[float, float, float, float]] = {}  # latest bbox per track

        # Last frame dimensions seen by update(). Stamped onto each emitted
        # VehicleAlert so the cloud can rescale `bbox` if the snapshot it
        # fetches is at a different resolution (e.g. shm-downscaled preview).
        self._last_frame_w: int = 0
        self._last_frame_h: int = 0

        # Cumulative counters
        self._total_entered: int = 0
        self._total_exited: int = 0

        # Tracks seen in zone last frame (for exit detection)
        self._prev_zone_ids: set[int] = set()

        # Smoothing: 5-frame median filter for vehicle count
        self._recent_counts: deque[int] = deque(maxlen=5)

        # Per-track last overstay-alert timestamp. Replaces the older
        # _alerted_overstay_ids set, which only fired once per visit and
        # produced alerts permanently frozen at exactly max_dwell_time_s.
        # Now we re-fire every cfg.OVERSTAY_REALERT_INTERVAL_S so the dwell
        # value tracks reality on long stays.
        self._last_overstay_alert_ts: dict[int, float] = {}

        # Overcrowding alert state
        self._overcrowding_active: bool = False
        self._last_overcrowding_alert_ts: float = 0.0

    # ------------------------------------------------------------------
    # Redis helpers — best-effort; never raise into the analytics loop
    # ------------------------------------------------------------------

    def _redis_key(self, tid: int) -> str:
        return f"vzone:entry:{self._cfg.zone_id}:{tid}"

    def _redis_get_entry(self, tid: int) -> Optional[float]:
        """Return the persisted entry timestamp for `tid`, or None."""
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(self._redis_key(tid))
            if raw is None:
                return None
            return float(raw)
        except Exception as e:
            if not self._redis_warned:
                _log.warning("Redis GET failed for zone %s: %s", self._cfg.zone_id, e)
                self._redis_warned = True
            return None

    def _redis_set_entry(self, tid: int, entry_ts: float) -> None:
        if self._redis is None:
            return
        try:
            ttl = max(60, int(self._cfg.max_dwell_time_s * cfg.OVERSTAY_ENTRY_TTL_MULTIPLIER))
            self._redis.set(self._redis_key(tid), f"{entry_ts:.3f}", ex=ttl)
        except Exception as e:
            if not self._redis_warned:
                _log.warning("Redis SET failed for zone %s: %s", self._cfg.zone_id, e)
                self._redis_warned = True

    def _zone_poly_frame_coords(self) -> Optional[list[list[float]]]:
        """Return this zone's polygon in source-frame pixel coords.

        Polygon is stored in canvas (Konva) coordinate space (CANVAS_W ×
        CANVAS_H). Stamping it onto each alert in *frame* space lets the
        cloud draw the zone outline on the saved evidence image without
        needing to know the edge's canvas dims or having to do a DB
        lookup per alert. Returns None if frame dims haven't been seen
        yet, or if the configured polygon is empty/degenerate.
        """
        poly_canvas = self._cfg.zone_poly or []
        if len(poly_canvas) < 3:
            return None
        if not self._last_frame_w or not self._last_frame_h:
            return None
        sx = self._last_frame_w / float(cfg.CANVAS_W)
        sy = self._last_frame_h / float(cfg.CANVAS_H)
        return [[float(x) * sx, float(y) * sy] for x, y in poly_canvas]

    def _redis_del_entry(self, tid: int) -> None:
        if self._redis is None:
            return
        try:
            self._redis.delete(self._redis_key(tid))
        except Exception:
            # Failure to clean up is harmless — TTL will reap it.
            pass

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def update_config(self, zone_config: ZoneConfig) -> None:
        """Hot-reload zone config (new polygons/thresholds)."""
        self._cfg = zone_config
        self._update_polygons(zone_config)

    def _update_polygons(self, zone_cfg: ZoneConfig) -> None:
        """Parse polygon points from config and reset the mask cache."""
        zp = zone_cfg.zone_poly or []
        self._zone_poly_canvas = (
            np.array(zp, dtype=np.float32).reshape(-1, 1, 2)
            if len(zp) >= 3 else None
        )
        # Invalidate per-frame-size mask cache; will be rebuilt lazily.
        self._mask_cache.clear()

    @property
    def zone_poly_canvas(self) -> Optional[np.ndarray]:
        """Polygon in canvas (Konva) coordinate space."""
        return self._zone_poly_canvas

    # ------------------------------------------------------------------
    # Mask lookup — O(1) replacement for cv2.pointPolygonTest
    # ------------------------------------------------------------------

    def _get_mask(self, frame_w: int, frame_h: int) -> Optional[np.ndarray]:
        if self._zone_poly_canvas is None:
            return None
        key = (frame_w, frame_h)
        mask = self._mask_cache.get(key)
        if mask is not None:
            return mask

        sx = frame_w / cfg.CANVAS_W
        sy = frame_h / cfg.CANVAS_H
        scaled = self._zone_poly_canvas.copy().astype(np.float64)
        scaled[:, 0, 0] *= sx
        scaled[:, 0, 1] *= sy
        poly_int = scaled.astype(np.int32)

        mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
        cv2.fillPoly(mask, [poly_int], 1)
        self._mask_cache[key] = mask
        return mask

    # ------------------------------------------------------------------
    # Per-frame analytics
    # ------------------------------------------------------------------

    def update(
        self,
        tracked_dets: list[tuple],
        frame_w: int,
        frame_h: int,
        ts: float,
        inf_ms: float,
        inf_fps: float,
    ) -> VehicleZoneMetrics:
        """
        Process one frame of tracked detections.

        tracked_dets: [(track_id, x1, y1, x2, y2, conf, class_id), ...]
        Returns a VehicleZoneMetrics object ready for MQTT publish.
        """
        mask = self._get_mask(frame_w, frame_h)

        # Remember frame dims so check_alerts() can stamp them on each emitted
        # VehicleAlert (cloud uses them to rescale `bbox` against the snapshot
        # it fetches, which may be a shm-downscaled copy).
        self._last_frame_w = int(frame_w)
        self._last_frame_h = int(frame_h)

        current_zone_ids: set[int] = set()
        current_classes: dict[int, int] = {}

        for item in tracked_dets:
            track_id, x1, y1, x2, y2, conf = item[:6]
            class_id = (
                item[6] if len(item) > 6 else cfg.DEFAULT_VEHICLE_CLASS_ID
            )

            cx = (x1 + x2) * 0.5
            cy = (y1 + y2) * 0.5

            in_zone = False
            if mask is not None:
                ix = int(cx)
                iy = int(cy)
                if 0 <= ix < frame_w and 0 <= iy < frame_h:
                    in_zone = bool(mask[iy, ix])

            if in_zone:
                current_zone_ids.add(track_id)
                current_classes[track_id] = class_id
                self._zone_bbox[track_id] = (float(x1), float(y1), float(x2), float(y2))
                if track_id not in self._zone_entry:
                    # First time this process has seen the track in this zone.
                    # Consult Redis: if a prior edge process recorded an entry
                    # timestamp for the same (zone, track_id) within the TTL,
                    # carry that forward so a restart doesn't reset dwell to 0.
                    persisted = self._redis_get_entry(track_id)
                    entry_ts = persisted if persisted is not None else ts
                    self._zone_entry[track_id] = entry_ts
                    self._zone_class[track_id] = class_id
                    self._total_entered += 1
                    # Refresh TTL on every (re-)entry so an actively-tracked
                    # vehicle's key doesn't expire mid-stay.
                    self._redis_set_entry(track_id, entry_ts)

        # Detect zone exits
        exited = self._prev_zone_ids - current_zone_ids
        for tid in exited:
            if tid in self._zone_entry:
                del self._zone_entry[tid]
                self._zone_class.pop(tid, None)
                self._zone_bbox.pop(tid, None)
                self._total_exited += 1
                self._last_overstay_alert_ts.pop(tid, None)
                self._redis_del_entry(tid)

        # Clean up stale entries
        for tid in list(self._zone_entry.keys()):
            if tid not in current_zone_ids and tid not in self._prev_zone_ids:
                del self._zone_entry[tid]
                self._zone_class.pop(tid, None)
                self._zone_bbox.pop(tid, None)
                self._last_overstay_alert_ts.pop(tid, None)
                self._redis_del_entry(tid)

        self._prev_zone_ids = current_zone_ids

        self._recent_counts.append(len(current_zone_ids))
        vehicle_count = int(np.median(self._recent_counts))

        type_counts: dict[str, int] = {}
        for tid in current_zone_ids:
            cls_id = self._zone_class.get(tid, current_classes.get(tid, 2))
            cls_name = cfg.VEHICLE_CLASS_NAMES.get(cls_id, f"class_{cls_id}")
            type_counts[cls_name] = type_counts.get(cls_name, 0) + 1

        if self._zone_entry:
            dwell_times = [ts - t for t in self._zone_entry.values()]
            avg_dwell = float(np.mean(dwell_times))
            max_dwell = float(np.max(dwell_times))
        else:
            avg_dwell = 0.0
            max_dwell = 0.0

        max_vehicles = self._cfg.max_vehicles
        occupancy_pct = min(100.0, (vehicle_count / max(max_vehicles, 1)) * 100.0)
        overcrowding_alert = vehicle_count > max_vehicles

        max_dwell_threshold = self._cfg.max_dwell_time_s
        overstay_ids = [
            tid for tid in current_zone_ids
            if tid in self._zone_entry
            and (ts - self._zone_entry[tid]) > max_dwell_threshold
        ]

        return VehicleZoneMetrics(
            edge_id=self._edge_id,
            zone_id=self._cfg.zone_id,
            camera_id=self._cfg.camera_id,
            ts=ts,
            vehicle_count=vehicle_count,
            vehicle_count_by_type=type_counts,
            max_vehicles=max_vehicles,
            occupancy_pct=round(occupancy_pct, 1),
            overstay_count=len(overstay_ids),
            avg_dwell_time_s=round(avg_dwell, 1),
            max_dwell_time_s=round(max_dwell, 1),
            total_entered=self._total_entered,
            total_exited=self._total_exited,
            active_track_count=len(tracked_dets),
            overcrowding_alert=overcrowding_alert,
            overstay_alert_ids=overstay_ids,
            inf_fps=round(inf_fps, 1),
            inf_ms=round(inf_ms, 1),
        )

    def check_alerts(self, metrics: VehicleZoneMetrics, ts: float) -> list[VehicleAlert]:
        """Generate edge-side overstay alerts.

        A given track fires its first overstay alert as soon as dwell crosses
        ``max_dwell_time_s``, then re-fires every ``OVERSTAY_REALERT_INTERVAL_S``
        while it remains in the zone. Each re-fire carries the current (larger)
        dwell value, so a long stay produces a sequence of alerts at growing
        dwell times instead of one row frozen at the threshold. The per-tid
        state is cleared when the vehicle leaves the zone.
        """
        alerts: list[VehicleAlert] = []
        realert_interval = float(cfg.OVERSTAY_REALERT_INTERVAL_S)

        for tid in metrics.overstay_alert_ids:
            last_alert_ts = self._last_overstay_alert_ts.get(tid)
            if last_alert_ts is not None and (ts - last_alert_ts) < realert_interval:
                continue
            dwell = ts - self._zone_entry.get(tid, ts)
            threshold = self._cfg.max_dwell_time_s
            level = "critical" if dwell > threshold * 2 else "warning"
            bbox = self._zone_bbox.get(tid)
            alerts.append(VehicleAlert(
                edge_id=self._edge_id,
                zone_id=self._cfg.zone_id,
                camera_id=self._cfg.camera_id,
                ts=ts,
                alert_type="overstay",
                level=level,
                message=(f"{self._cfg.name}: Vehicle #{tid} has been "
                         f"in zone for {_fmt_dwell(dwell)} "
                         f"(limit: {_fmt_dwell(threshold)})"),
                dwell_time_s=round(dwell, 1),
                track_id=tid,
                bbox=list(bbox) if bbox else None,
                frame_w=self._last_frame_w or None,
                frame_h=self._last_frame_h or None,
                zone_poly=self._zone_poly_frame_coords(),
            ))
            self._last_overstay_alert_ts[tid] = ts

        return alerts
