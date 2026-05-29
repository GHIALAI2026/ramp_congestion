"""Owns the cross-process pipeline plumbing for the edge agent.

In addition to tracking active CameraStream instances, this manager:

  * Allocates a fixed-size camera slot table backed by ``multiprocessing.Array``
    so the analytics workers can read viewer counts lock-free.
  * Owns the SharedMemory frame and JPEG registries.
  * Spawns + tears down the AnalyticsPool and UIRendererPool.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from typing import Optional

from edge_agent import config as cfg
from edge_agent.pipeline.analytics_worker import AnalyticsPool
from edge_agent.pipeline.camera_stream import CameraStream
from edge_agent.pipeline.cpu_topology import plan_layout
from edge_agent.pipeline.det_ring import DetectionRing
from edge_agent.pipeline.rtsp_probe import get_rtsp_fps
from edge_agent.pipeline.shm_buffer import (
    FrameBufferRegistry,
    JPEGBufferRegistry,
)
from edge_agent.pipeline.ui_renderer import UIRendererPool
from edge_agent.schemas import ZoneConfig

logger = logging.getLogger(__name__)


class CameraManager:
    def __init__(
        self,
        engine,
        frame_registry: FrameBufferRegistry,
        jpeg_registry: JPEGBufferRegistry,
    ):
        self._engine = engine
        self._lock = threading.Lock()
        self._streams: dict[str, CameraStream] = {}

        # Slot allocation — viewer counter Array is sized once.
        self._max_slots = max(1, int(cfg.MAX_CAMERA_SLOTS))
        self._viewer_array = multiprocessing.Array("i", self._max_slots, lock=True)
        self._slot_by_cam: dict[str, int] = {}
        self._free_slots: list[int] = list(range(self._max_slots))[::-1]
        self._slot_lock = threading.Lock()

        self._frame_registry = frame_registry
        self._jpeg_registry = jpeg_registry

        # CPU layout — assigns P-cores to iter/shards, E-cores to UI/HTTP.
        self._cpu_layout = plan_layout(cfg.NUM_ANALYTICS_SHARDS)

        # Detection ring — lock-free SHM channel for per-frame detections.
        # Sized to MAX_CAMERA_SLOTS so the cam slot index doubles as the
        # ring slot index.
        self._det_ring = DetectionRing(
            num_slots=self._max_slots,
            max_dets=cfg.DET_RING_MAX_DETS,
            ring_depth=cfg.DET_RING_DEPTH,
        )
        self._det_ring_view = self._det_ring.view()

        # Renderer pool — owns its own task queue.
        self._ui_pool = UIRendererPool(
            num_workers=cfg.NUM_UI_WORKERS,
            cpu_set=self._cpu_layout.worker_set,
        )
        # Analytics pool — feeds the renderer pool's task queue.
        self._analytics_pool = AnalyticsPool(
            num_shards=cfg.NUM_ANALYTICS_SHARDS,
            ui_signal_queue=self._ui_pool.task_queue,
            viewer_array=self._viewer_array,
            cpu_layout=self._cpu_layout,
            det_ring_view=self._det_ring_view,
        )

        # Let the engine skip the SHM memcpy when no viewer is registered.
        # The check resolves cam_id → slot_idx → viewer_array[slot_idx].
        if hasattr(engine, "set_viewer_check_fn"):
            engine.set_viewer_check_fn(self._has_viewer_for_cam)
        if hasattr(engine, "set_iter_cpu_set"):
            engine.set_iter_cpu_set(self._cpu_layout.iter_set)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def frame_registry(self) -> FrameBufferRegistry:
        return self._frame_registry

    @property
    def jpeg_registry(self) -> JPEGBufferRegistry:
        return self._jpeg_registry

    def start_workers(self) -> None:
        """Start analytics + UI worker pools.

        Must be called *after* all initial cameras are registered so the
        worker processes inherit the fully-populated SharedMemory dicts.
        """
        self._ui_pool.start()
        self._analytics_pool.start()

    def stop_all(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for s in streams:
            try:
                s.stop()
            except Exception:
                logger.exception("Error stopping camera stream")
        self._analytics_pool.stop()
        self._ui_pool.stop()
        self._frame_registry.shutdown()
        self._jpeg_registry.shutdown()
        self._det_ring.close()

    # ------------------------------------------------------------------
    # Camera CRUD
    # ------------------------------------------------------------------

    def add_camera(
        self,
        camera_id: str,
        source_url: str,
        zones: list[ZoneConfig],
    ) -> None:
        with self._lock:
            if camera_id in self._streams:
                logger.info("Camera %s already running, skipping", camera_id)
                return
            slot_idx = self._allocate_slot(camera_id)
            if slot_idx < 0:
                logger.error(
                    "Camera %s rejected — no free slots (max=%d)",
                    camera_id, self._max_slots,
                )
                return
            # Pre-allocate SHM segments before any data flows.
            frame_slot = self._frame_registry.register(camera_id)
            jpeg_slot = self._jpeg_registry.register(camera_id)
            self._ui_pool.register_camera(camera_id, frame_slot, jpeg_slot)

            stream = CameraStream(
                camera_id,
                source_url,
                zones,
                slot_idx,
                self._engine,
                self._analytics_pool,
                self._viewer_array,
            )
            self._streams[camera_id] = stream

        try:
            stream.start()
            logger.info("Added camera %s (%s, slot=%d)",
                        camera_id, source_url, slot_idx)
        except Exception:
            logger.exception("Camera %s start failed", camera_id)

    def sync_camera(
        self,
        camera_id: str,
        source_url: str,
        zones: list[ZoneConfig],
    ) -> None:
        with self._lock:
            stream = self._streams.get(camera_id)
        if stream is None:
            self.add_camera(camera_id, source_url, zones)
            return
        stream.update_zones(zones)
        logger.info("Synced camera %s with %d zones", camera_id, len(zones))

    def remove_camera(self, camera_id: str) -> None:
        with self._lock:
            stream = self._streams.pop(camera_id, None)
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logger.exception("Error stopping camera %s", camera_id)
        # Release SHM + slot only after the stream is stopped.
        self._ui_pool.unregister_camera(camera_id)
        self._frame_registry.unregister(camera_id)
        self._jpeg_registry.unregister(camera_id)
        self._release_slot(camera_id)
        logger.info("Removed camera %s", camera_id)

    def update_zone(self, zone_config: ZoneConfig) -> None:
        with self._lock:
            stream = self._streams.get(zone_config.camera_id)
        if stream:
            stream.upsert_zone(zone_config)
        else:
            logger.warning("Zone update for unknown camera %s", zone_config.camera_id)

    # ------------------------------------------------------------------
    # Snapshots / viewers — used by HTTP server
    # ------------------------------------------------------------------

    def get_snapshot(self, camera_id: str) -> Optional[bytes]:
        with self._lock:
            stream = self._streams.get(camera_id)
        if stream is None:
            return None
        frame = stream.get_snapshot_frame()
        if frame is None:
            return None
        import cv2
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def get_annotated_snapshot(self, camera_id: str) -> Optional[bytes]:
        slot = self._jpeg_registry.get(camera_id)
        if slot is None:
            return None
        jpeg, ts = slot.read()
        if jpeg is None:
            return None
        import time
        if (time.time() - ts) > cfg.LIVE_FRAME_STALE_S:
            return None
        return jpeg

    def get_annotated_snapshot_with_ts(
        self, camera_id: str,
    ) -> Optional[tuple[bytes, float]]:
        slot = self._jpeg_registry.get(camera_id)
        if slot is None:
            return None
        jpeg, ts = slot.read()
        if jpeg is None:
            return None
        return jpeg, ts

    def get_camera_stats(self) -> dict[str, dict]:
        """Per-camera live stats for the dashboard cameras page.

        Returns a dict keyed by camera_id with {width, height, fps}.

        ``fps`` is the camera's *declared* RTSP frame rate (probed via
        ffprobe and cached), NOT the post-inference rate — the latter is
        gated to TARGET_FPS by the iter thread and tells the operator
        nothing about what the camera is actually publishing. Probes
        run in the background; cameras whose probe hasn't completed yet
        report fps=None.

        Cameras that have not produced an inferred frame yet have
        null width/height so the UI can render them consistently.
        """
        with self._lock:
            streams = list(self._streams.items())
        out: dict[str, dict] = {}
        for cam_id, stream in streams:
            res = None
            try:
                if hasattr(self._engine, "get_cam_resolution"):
                    res = self._engine.get_cam_resolution(cam_id)
            except Exception:
                pass
            w, h = (res or (None, None))
            source_url = getattr(stream, "_source_url", None)
            rtsp_fps = get_rtsp_fps(source_url) if source_url else None
            out[cam_id] = {
                "width": w,
                "height": h,
                "fps": round(rtsp_fps, 1) if rtsp_fps is not None else None,
            }
        return out

    def add_viewer(self, camera_id: str) -> bool:
        with self._lock:
            stream = self._streams.get(camera_id)
        if stream is None:
            return False
        stream.add_viewer()
        return True

    def remove_viewer(self, camera_id: str) -> None:
        with self._lock:
            stream = self._streams.get(camera_id)
        if stream is not None:
            stream.remove_viewer()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def total_frames_tracked(self) -> int:
        return self._analytics_pool.total_frames_tracked()

    def get_stats(self) -> dict:
        with self._lock:
            cameras_assigned = len(self._streams)
            cameras_active = sum(
                1 for s in self._streams.values() if s.has_fresh_inference
            )
            cameras_errored = sum(
                1 for s in self._streams.values() if s.health_problem
            )
            zones_active = sum(s.zone_count for s in self._streams.values())
            error_cams = [
                cid for cid, s in self._streams.items() if s.health_problem
            ]
        return {
            "cameras_active": cameras_active,
            "cameras_assigned": cameras_assigned,
            "cameras_errored": cameras_errored,
            "zones_active": zones_active,
            "error_cameras": error_cams,
        }

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------

    def _has_viewer_for_cam(self, camera_id: str) -> bool:
        """Resolve cam_id → viewer count without acquiring self._lock.

        Called from the Voyager iter thread on every kept frame, so this
        path must stay short. The slot map is dict-thread-safe for reads;
        the multiprocessing.Array indexer takes its own internal lock.
        """
        slot = self._slot_by_cam.get(camera_id)
        if slot is None:
            return False
        try:
            return self._viewer_array[slot] > 0
        except Exception:
            return False

    def _allocate_slot(self, camera_id: str) -> int:
        with self._slot_lock:
            existing = self._slot_by_cam.get(camera_id)
            if existing is not None:
                return existing
            if not self._free_slots:
                return -1
            slot = self._free_slots.pop()
            self._slot_by_cam[camera_id] = slot
            with self._viewer_array.get_lock():
                self._viewer_array[slot] = 0
            return slot

    def _release_slot(self, camera_id: str) -> None:
        with self._slot_lock:
            slot = self._slot_by_cam.pop(camera_id, None)
            if slot is None:
                return
            with self._viewer_array.get_lock():
                self._viewer_array[slot] = 0
            self._free_slots.append(slot)
