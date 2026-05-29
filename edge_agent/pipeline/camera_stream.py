"""CameraStream: per-camera pipeline orchestrator for vehicle detection.

After the multiprocess refactor, this class is intentionally thin:

  * Owns the lazy ``VideoDecoder`` (only spun up if Voyager goes stale).
  * Holds the current zone config, source URL, and viewer counter slot.
  * On Voyager callbacks, fans the small payload out to:
      - the AnalyticsPool (no pixels, just dets + dimensions)
      - nothing else; pixels travel via the per-camera SharedMemory slot
        that the Voyager engine writes directly.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from edge_agent import config as cfg
from edge_agent.pipeline.video_decoder import VideoDecoder, rtsp_preflight
from edge_agent.schemas import ZoneConfig
from rtsp_utils import normalize_rtsp_url

logger = logging.getLogger(__name__)


class CameraStream:
    """Per-camera glue between Voyager, analytics shards, and the viewer counter."""

    def __init__(
        self,
        camera_id: str,
        source_url: str,
        zones: list[ZoneConfig],
        slot_idx: int,
        engine,                   # VoyagerEngine
        analytics_pool,           # AnalyticsPool
        viewer_array,             # multiprocessing.Array of int counts
    ):
        self._camera_id = camera_id
        self._source_url = normalize_rtsp_url(source_url)
        self._slot_idx = slot_idx
        self._engine = engine
        self._analytics_pool = analytics_pool
        self._viewer_array = viewer_array

        self._fallback_decoder = VideoDecoder(self._source_url, camera_id)

        self._zones_lock = threading.Lock()
        self._current_zones = list(zones)

        self._running = False
        self._errored = False
        self._voyager_started = False
        self._startup_lock = threading.Lock()
        self._retry_thread: Optional[threading.Thread] = None
        self._started_ts: float = 0.0

        self._frame_times: deque[float] = deque(maxlen=30)
        self._last_input_ts: float = 0.0
        self._last_voyager_check_ts: float = 0.0

        from edge_agent.pipeline.voyager_engine import VoyagerEngine
        self._use_voyager = isinstance(engine, VoyagerEngine)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._started_ts = time.time()

        # Two-stage preflight: TCP reach + ffprobe codec validation. The
        # codec stage protects against cameras that listen on 554 but
        # stream invalid video — those would otherwise wedge the Voyager
        # SDK call for 30-60 s, holding the engine deploy lock and
        # blocking every other camera's startup.
        if cfg.ENABLE_SOURCE_PREFLIGHT:
            ok, reason = rtsp_preflight(
                self._source_url,
                cfg.PREFLIGHT_TCP_TIMEOUT_S,
                cfg.RTSP_CODEC_PREFLIGHT_TIMEOUT_S,
            )
            if not ok:
                if self._use_voyager:
                    logger.warning(
                        "[%s] preflight failed (%s) for %s; deferring Voyager start, will retry in background",
                        self._camera_id, reason, self._source_url,
                    )
                    self._schedule_source_retry()
                else:
                    self._errored = True
                    logger.error(
                        "[%s] preflight failed (%s) for %s; skipping startup",
                        self._camera_id, reason, self._source_url,
                    )
                return
        self._errored = False
        self._publish_zone_config()
        self._start_voyager_stream()
        logger.info(
            "[%s] Started (source=%s, voyager=%s, zones=%d)",
            self._camera_id, self._source_url, self._use_voyager,
            len(self._current_zones),
        )

    def stop(self) -> None:
        self._running = False
        self._fallback_decoder.stop()
        if self._use_voyager:
            self._engine.remove_stream(self._camera_id)
        self._analytics_pool.remove_camera(self._camera_id)
        logger.info("[%s] Stopped", self._camera_id)

    @property
    def slot_idx(self) -> int:
        return self._slot_idx

    @property
    def zone_count(self) -> int:
        return len(self._current_zones)

    # ------------------------------------------------------------------
    # Zone config
    # ------------------------------------------------------------------

    def update_zones(self, zones: list[ZoneConfig]) -> None:
        with self._zones_lock:
            self._current_zones = list(zones)
        self._publish_zone_config()
        logger.info("[%s] Zones updated: %s", self._camera_id,
                    [z.zone_id for z in zones])

    def upsert_zone(self, zone_config: ZoneConfig) -> None:
        with self._zones_lock:
            new_zones = [z for z in self._current_zones if z.zone_id != zone_config.zone_id]
            new_zones.append(zone_config)
            self._current_zones = new_zones
            zones_snapshot = list(new_zones)
        self._analytics_pool.update_config(self._camera_id, self._slot_idx, zones_snapshot)
        logger.info("[%s] Zone upserted: %s", self._camera_id, zone_config.zone_id)

    def _publish_zone_config(self) -> None:
        with self._zones_lock:
            zones_snapshot = list(self._current_zones)
        self._analytics_pool.update_config(
            self._camera_id, self._slot_idx, zones_snapshot,
        )

    # ------------------------------------------------------------------
    # Viewer counting
    # ------------------------------------------------------------------

    def add_viewer(self) -> None:
        with self._viewer_array.get_lock():
            self._viewer_array[self._slot_idx] += 1

    def remove_viewer(self) -> None:
        with self._viewer_array.get_lock():
            v = self._viewer_array[self._slot_idx]
            if v > 0:
                self._viewer_array[self._slot_idx] = v - 1

    @property
    def has_viewers(self) -> bool:
        return self._viewer_array[self._slot_idx] > 0

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def mark_errored(self) -> None:
        # Don't churn if we already know we're errored.
        if self._errored:
            return
        self._errored = True
        logger.warning(
            "[%s] marked errored — detaching from Voyager and scheduling retry "
            "(source=%s)",
            self._camera_id, self._source_url,
        )
        # Detach the dead stream from the engine so the NPU pipeline slot is
        # released and a fresh add_stream() can succeed. Without this the
        # retry thread sees _voyager_started=True and silently no-ops, which
        # was the overnight failure mode that left 11 cams stuck in error.
        if self._use_voyager and self._voyager_started:
            try:
                self._engine.remove_stream(self._camera_id)
            except Exception as exc:
                logger.warning(
                    "[%s] engine.remove_stream during mark_errored failed: %s",
                    self._camera_id, exc,
                )
            with self._startup_lock:
                self._voyager_started = False
        if self._running:
            self._schedule_source_retry()

    @property
    def errored(self) -> bool:
        return self._errored

    @property
    def has_fresh_inference(self) -> bool:
        if not self._voyager_started or not self._last_input_ts:
            return False
        return (time.time() - self._last_input_ts) <= cfg.INFERENCE_HEALTH_STALE_S

    @property
    def health_problem(self) -> bool:
        if not self._running:
            return True
        if self.has_fresh_inference:
            return False
        if (self._started_ts and
                (time.time() - self._started_ts) < cfg.INFERENCE_STARTUP_GRACE_S):
            return False
        return self._errored or self._use_voyager

    @property
    def inf_fps(self) -> float:
        if len(self._frame_times) < 2:
            return 0.0
        diffs = [self._frame_times[i] - self._frame_times[i - 1]
                 for i in range(1, len(self._frame_times))]
        avg_interval = sum(diffs) / len(diffs)
        return 1.0 / avg_interval if avg_interval > 0 else 0.0

    # ------------------------------------------------------------------
    # Snapshots (HTTP read-path)
    # ------------------------------------------------------------------

    def get_snapshot_frame(self) -> Optional[np.ndarray]:
        """Return the freshest raw frame, falling back to the lazy decoder."""
        if self._use_voyager:
            frame = self._engine.get_snapshot_frame(self._camera_id)
            if frame is not None:
                if self._is_local_source():
                    self._errored = False
                # Voyager is alive — make sure we're not running the fallback.
                if self._fallback_decoder.is_running:
                    self._fallback_decoder.stop()
                return frame

        # Voyager has not delivered a fresh frame; spin up the fallback if
        # it's not already running. Only do this for HTTP requests; the
        # analytics path never touches it.
        if not self._fallback_decoder.is_running:
            self._fallback_decoder.start()
        return self._fallback_decoder.get_snapshot()

    # ------------------------------------------------------------------
    # Voyager callback (called from the iter thread)
    # ------------------------------------------------------------------

    def on_frame(
        self,
        detections: list,
        frame_w: int,
        frame_h: int,
        ts: float,
        inf_ms: float,
        fps: float,
    ) -> None:
        """Cheap fan-out: the iter thread MUST stay fast.

        The frame pixels are already in SharedMemory by the time Voyager
        invokes this callback — we only forward the small payload to the
        analytics shard.
        """
        self._frame_times.append(ts)
        self._last_input_ts = ts
        self._errored = False
        self._analytics_pool.submit_data(
            self._camera_id, detections, frame_w, frame_h, ts, inf_ms, fps,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_local_source(self) -> bool:
        source = self._source_url.lower()
        return "localhost" in source or "127.0.0.1" in source

    def _start_voyager_stream(self) -> None:
        if not self._use_voyager:
            return
        with self._startup_lock:
            if self._voyager_started or not self._running:
                return
            self._engine.add_stream(
                self._camera_id,
                self._source_url,
                self.on_frame,
                error_callback=self.mark_errored,
            )
            self._voyager_started = True

    def _schedule_source_retry(self) -> None:
        """Background daemon that probes a failed source until it is
        decodable, then starts the Voyager pipeline. Runs indefinitely
        while the camera is registered; stops on stream.stop().
        """
        if self._retry_thread and self._retry_thread.is_alive():
            return
        self._retry_thread = threading.Thread(
            target=self._retry_source_start,
            daemon=True,
            name=f"SourceRetry-{self._camera_id}",
        )
        self._retry_thread.start()

    def _retry_source_start(self) -> None:
        backoff_s = 1.0
        max_backoff_s = max(1.0, float(cfg.SOURCE_RETRY_MAX_INTERVAL_S))
        while self._running and not self._voyager_started:
            time.sleep(backoff_s)
            ok, reason = rtsp_preflight(
                self._source_url,
                cfg.PREFLIGHT_TCP_TIMEOUT_S,
                cfg.RTSP_CODEC_PREFLIGHT_TIMEOUT_S,
            )
            if not ok:
                logger.debug(
                    "[%s] retry probe still failing (%s); sleeping %.1fs",
                    self._camera_id, reason, backoff_s,
                )
                backoff_s = min(max_backoff_s, backoff_s * 2.0)
                continue
            logger.info(
                "[%s] Source healthy after retry; starting Voyager pipeline for %s",
                self._camera_id, self._source_url,
            )
            self._errored = False
            self._publish_zone_config()
            self._start_voyager_stream()
            if self._voyager_started:
                logger.info(
                    "[%s] Started after retry (source=%s, zones=%d)",
                    self._camera_id, self._source_url, len(self._current_zones),
                )
                return
            # Voyager start refused — back off and try the whole cycle again.
            backoff_s = min(max_backoff_s, backoff_s * 2.0)
