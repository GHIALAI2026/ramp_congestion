"""
Voyager SDK inference engine — Axelera Metis AIPU.

Owns the single InferenceStream with N pipelines (one per AIPU grouping).
Cameras are distributed across pipelines via add_source(), each pipeline
handling up to MAX_SOURCES_PER_PIPELINE sources.

The iteration thread is the one true metering point. It does the
absolute minimum:

  1. Pull frame_result from the stream.
  2. Filter detections (class, confidence, bbox area, aspect ratio).
  3. Hand frame pixels to the camera's SharedMemory FrameSlot
     (one memcpy, no pickling).
  4. Hand the small (cam_id, dets, w, h, ts, …) tuple to the
     per-camera callback registered by CameraStream.

NMS is dropped here — Voyager already produces post-NMS detections.
RGB→BGR conversion is dropped from the hot path: the Voyager SDK
``image.asarray('BGR')`` call returns BGR directly when supported.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

from edge_agent import config as cfg
from edge_agent.pipeline.shm_buffer import FrameBufferRegistry, FrameSlot

logger = logging.getLogger(__name__)


MIN_BBOX_AREA = cfg.MIN_VEHICLE_BBOX_AREA
MAX_BBOX_AREA = cfg.MAX_VEHICLE_BBOX_AREA
MIN_ASPECT = cfg.MIN_VEHICLE_ASPECT
MAX_ASPECT = cfg.MAX_VEHICLE_ASPECT

MAX_SOURCES_PER_PIPELINE = max(
    1, int(getattr(cfg, "VOYAGER_MAX_SOURCES_PER_PIPELINE", 10))
)


# Detection: (x1, y1, x2, y2, conf, class_id)
Detection = tuple[float, float, float, float, float, int]


def _as_bgr(image, path: str) -> Optional[np.ndarray]:
    """Convert a Voyager image into a BGR ndarray on the cached path.

    ``path`` is the per-camera memoized choice from :func:`_probe_asarray_path`
    (``"BGR"`` when the SDK accepts the format hint, else ``"default"``).
    Avoids the per-frame try/except probe on the iter-thread hot path.
    """
    if path == "BGR":
        arr = image.asarray("BGR")
    else:
        arr = image.asarray()
    if arr is None or arr.ndim != 3 or arr.shape[2] != 3:
        return arr
    return arr


def _probe_asarray_path(image) -> tuple[Optional[np.ndarray], str]:
    """First-frame probe: pick the cheapest asarray path for this SDK build.

    Returns (frame_ndarray, path). The path is cached per camera and
    reused for every subsequent frame.
    """
    try:
        arr = image.asarray("BGR")
        return arr, "BGR"
    except TypeError:
        pass
    arr = image.asarray()
    return arr, "default"


def _detect_sub_devices() -> int:
    """Detect AIPU sub-device count from axdevice."""
    try:
        result = subprocess.run(
            ["axdevice"], capture_output=True, text=True, timeout=5)
        m = re.search(r"mvm=(\d+)-(\d+)", result.stdout)
        if m:
            return int(m.group(2)) - int(m.group(1)) + 1
    except Exception:
        pass
    return 4  # safe default for Metis M.2 Max


# Callback signature: cb(detections, frame_w, frame_h, ts, inf_ms, fps)
FrameCallback = Callable[[list, int, int, float, float, float], None]


class VoyagerEngine:
    """
    Single InferenceStream, N pipelines (one per AIPU grouping).

    Cameras are distributed across pipelines via add_source(). Each
    pipeline handles up to MAX_SOURCES_PER_PIPELINE cameras. The
    iteration thread writes pixels into the camera's SharedMemory slot
    and forwards a small payload to the registered callback.
    """

    def __init__(
        self,
        network: str | None = None,
        conf: float = 0.25,
        iou: float = 0.45,
        frame_registry: Optional[FrameBufferRegistry] = None,
    ):
        # Fail-closed on software decode. Falling back to libavcodec on
        # the CPU cannot keep up with 40 × 1080p RTSP streams on this
        # box; the system would silently crawl rather than error.
        if not cfg.VOYAGER_ALLOW_HARDWARE_CODEC or not cfg.VOYAGER_ENABLE_VAAPI:
            if os.environ.get("VOYAGER_ALLOW_SOFTWARE_FALLBACK", "0") != "1":
                raise RuntimeError(
                    "Hardware video decoding is disabled "
                    f"(VOYAGER_ALLOW_HARDWARE_CODEC={cfg.VOYAGER_ALLOW_HARDWARE_CODEC}, "
                    f"VOYAGER_ENABLE_VAAPI={cfg.VOYAGER_ENABLE_VAAPI}). "
                    "Software decoding cannot scale to the production stream "
                    "count on this hardware. Set "
                    "VOYAGER_ALLOW_SOFTWARE_FALLBACK=1 to override (testing only)."
                )

        self._network = network or cfg.VOYAGER_NETWORK
        self._conf = conf
        self._iou = iou
        self._num_sub_devices = _detect_sub_devices()
        self._vehicle_class_ids = set(cfg.VEHICLE_CLASS_IDS)
        # Boolean lookup table indexed by class_id — replaces a per-detection
        # `int in set` check in the iter-thread hot path. Sized to one past
        # the largest configured vehicle class id; out-of-range ids fall
        # through the bounds mask below.
        if self._vehicle_class_ids:
            _max_cls = max(self._vehicle_class_ids) + 1
            self._vehicle_class_lut = np.zeros(_max_cls, dtype=bool)
            for _cid in self._vehicle_class_ids:
                self._vehicle_class_lut[_cid] = True
        else:
            self._vehicle_class_lut = np.zeros(0, dtype=bool)
        self._aipu_cores = max(1, int(getattr(cfg, "VOYAGER_AIPU_CORES", 1)))
        self._max_pipelines = max(1, self._num_sub_devices // self._aipu_cores)

        self._frame_registry = frame_registry

        self._lock = threading.Lock()
        self._callbacks: dict[str, FrameCallback] = {}
        self._error_callbacks: dict[str, callable] = {}
        self._running: dict[str, bool] = {}
        self._dropped_callbacks: dict[str, int] = {}
        self._drop_log_ts: dict[str, float] = {}

        # stream_id ↔ cam_id mapping
        self._sid_to_cam: dict[int, str] = {}
        self._cam_to_sid: dict[str, int] = {}
        # stream_id → pipeline index, plus the most recent frame timestamp
        # per pipeline. Used by _check_pipeline_staleness to detect when a
        # single Voyager pipeline wedges while peers stay healthy — the
        # aggregate throughput watchdog in start.sh can't see that because
        # the other pipelines keep the throughput log growing.
        self._sid_to_pipeline_idx: dict[int, int] = {}
        self._pipeline_last_frame_ts: dict[int, float] = {}
        # Per-camera dedupe for the source-staleness watchdog. The watchdog
        # invokes a camera's error_callback (= CameraStream.mark_errored)
        # when a single source stops delivering frames while its pipeline
        # peers stay healthy — Voyager has been observed to swallow the
        # underlying FrameEvent(source_error, ...) and log it as "Ignoring
        # event …", leaving the dead source attached forever. Cleared on
        # every fresh frame from the camera.
        self._last_camera_recovery_ts: dict[str, float] = {}

        self._stream = None
        self._pipelines: list[object] = []
        self._pipeline_source_counts: list[int] = []
        self._pipeline_broken: set[int] = set()
        self._stream_thread: threading.Thread | None = None
        self._deploy_lock = threading.Lock()

        # Per-camera FPS tracking — owned by the iteration thread; readers
        # grab the current snapshot via thread-safe dict lookups.
        self._cam_fps: dict[str, float] = {}
        self._cam_frame_counts: dict[str, int] = {}
        self._cam_fps_t0: dict[str, float] = {}
        # Per-camera frame resolution (width, height) — updated on the iter
        # thread from frame_result.image dims. Readers (HTTP /camera_stats)
        # do a thread-safe dict lookup.
        self._cam_resolution: dict[str, tuple[int, int]] = {}

        # Iter-thread frame-rate gate. The Voyager SDK is told the target
        # rate via specified_frame_rate, but we don't trust it to drop
        # upstream of the callback — gate here so we skip the SHM memcpy,
        # detection extract, and downstream pickle on dropped frames.
        # The epsilon is the jitter window: an inter-arrival up to this
        # much below the nominal interval still counts as on-time. With
        # TARGET_FPS=8 (=125 ms nominal) and Metis showing ±10-20 ms
        # jitter in observed deliveries, 5 ms was too tight — measured
        # gate-drop rate was ~33 % at steady state. 20 ms absorbs the
        # observed jitter while still throwing out grossly-early bursts.
        self._frame_min_interval = 1.0 / max(1.0, float(cfg.TARGET_FPS))
        self._frame_gate_epsilon_s = 0.020
        self._last_cb_ts: dict[str, float] = {}

        # Viewer-gated SHM write. When set, the iter thread only memcpys
        # the frame into the per-camera SHM slot when a viewer is
        # registered for that camera (or within the grace window after
        # the last viewer disconnected). Defaults to None which preserves
        # the original always-write behavior.
        self._viewer_check_fn: Optional[Callable[[str], bool]] = None
        self._viewer_grace_s = float(getattr(cfg, "FRAME_VIEWER_GRACE_S", 30.0))
        self._last_viewer_seen_ts: dict[str, float] = {}

        # Color format detection — set on first frame per camera.
        self._needs_rgb_swap: dict[str, bool] = {}
        # Memoized asarray path per camera ("BGR" or "default"). Probed
        # once on the first viewer-gated frame, reused thereafter to
        # skip the per-frame try/except on the iter-thread hot path.
        self._asarray_path: dict[str, str] = {}

        # CPU set for the iter thread; set by CameraManager via
        # set_iter_cpu_set(). Empty means leave to the OS scheduler.
        self._iter_cpu_set: set[int] = set()

        # Global rolling stats for heartbeat
        self._inf_times: deque[float] = deque(maxlen=120)

        # Monotonic count of frames returned from Metis. Incremented once
        # per iter callback (single writer; GIL makes the int store atomic).
        self._total_frames_inferenced: int = 0

        max_total = self._max_pipelines * MAX_SOURCES_PER_PIPELINE
        logger.info(
            "VoyagerEngine created — network=%s, conf=%.2f, iou=%.2f, "
            "sub_devices=%d, aipu_cores=%d, max_pipelines=%d, "
            "max_streams=%d, vehicle_classes=%s",
            self._network, self._conf, self._iou,
            self._num_sub_devices, self._aipu_cores,
            self._max_pipelines, max_total,
            list(self._vehicle_class_ids),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_stream(
        self,
        cam_id: str,
        source_url: str,
        callback: FrameCallback,
        error_callback: callable = None,
    ) -> None:
        """Add a camera as a source.

        callback signature: cb(detections, frame_w, frame_h, ts, inf_ms, fps)
        """
        with self._lock:
            if cam_id in self._running and self._running[cam_id]:
                logger.warning("[Voyager] %s already has a stream", cam_id)
                return
            total = (sum(self._pipeline_source_counts)
                     if self._pipeline_source_counts else 0)
            max_total = self._max_pipelines * MAX_SOURCES_PER_PIPELINE
            if total >= max_total:
                logger.error(
                    "[Voyager] %s rejected — at capacity (%d/%d streams)",
                    cam_id, total, max_total,
                )
                return
            self._callbacks[cam_id] = callback
            self._error_callbacks[cam_id] = error_callback
            self._running[cam_id] = True
            self._cam_fps[cam_id] = 0.0
            self._cam_frame_counts[cam_id] = 0
            now_add = time.time()
            self._cam_fps_t0[cam_id] = now_add
            # Prime the per-camera staleness clock at add_stream time so a
            # source that never produces a frame eventually trips the
            # source-staleness watchdog and gets retried (otherwise
            # _last_cb_ts would only get an entry on the first frame, and
            # the watchdog would have nothing to compare).
            self._last_cb_ts[cam_id] = now_add

        t = threading.Thread(
            target=self._add_source,
            args=(cam_id, source_url),
            daemon=True,
            name=f"Voyager-deploy-{cam_id}",
        )
        t.start()
        logger.info("[Voyager] %s stream started → %s", cam_id, source_url)

    def remove_stream(self, cam_id: str) -> None:
        sid = None
        with self._lock:
            self._running[cam_id] = False
            sid = self._cam_to_sid.pop(cam_id, None)
            if sid is not None:
                self._sid_to_cam.pop(sid, None)
            self._callbacks.pop(cam_id, None)
            self._error_callbacks.pop(cam_id, None)
            self._cam_fps.pop(cam_id, None)
            self._cam_frame_counts.pop(cam_id, None)
            self._cam_fps_t0.pop(cam_id, None)
            self._cam_resolution.pop(cam_id, None)
            self._last_cb_ts.pop(cam_id, None)
            self._last_camera_recovery_ts.pop(cam_id, None)
            self._last_viewer_seen_ts.pop(cam_id, None)
            self._needs_rgb_swap.pop(cam_id, None)
            self._asarray_path.pop(cam_id, None)
        if sid is not None:
            with self._deploy_lock:
                for idx, pipe in enumerate(self._pipelines):
                    if sid in pipe.sources:
                        try:
                            pipe.remove_source(sid)
                            self._pipeline_source_counts[idx] -= 1
                            self._sid_to_pipeline_idx.pop(sid, None)
                            logger.info(
                                "[Voyager] %s source removed (stream_id=%d, pipeline=%d)",
                                cam_id, sid, idx,
                            )
                        except Exception as e:
                            logger.warning(
                                "[Voyager] %s remove_source error: %s", cam_id, e,
                            )
                        break

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._cam_to_sid.keys())
        for cam_id in ids:
            self.remove_stream(cam_id)
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass
            self._stream = None
            self._pipelines.clear()
            self._pipeline_source_counts.clear()
            self._pipeline_broken.clear()

    def get_snapshot_frame(self, cam_id: str) -> Optional[np.ndarray]:
        """Return the latest raw frame from the SharedMemory slot."""
        if self._frame_registry is None:
            return None
        slot: Optional[FrameSlot] = self._frame_registry.get(cam_id)
        if slot is None:
            return None
        frame, ts = slot.read()
        if frame is None:
            return None
        if ts and (time.time() - ts) > cfg.LIVE_FRAME_STALE_S:
            return None
        return frame

    @property
    def total_frames_inferenced(self) -> int:
        return self._total_frames_inferenced

    @property
    def avg_inf_ms(self) -> float:
        with self._lock:
            if not self._inf_times:
                return 0.0
            return sum(self._inf_times) / len(self._inf_times)

    @property
    def avg_inf_fps(self) -> float:
        with self._lock:
            if not self._cam_fps:
                return 0.0
            return sum(self._cam_fps.values())

    def get_cam_fps(self, cam_id: str) -> float:
        return self._cam_fps.get(cam_id, 0.0)

    def get_cam_resolution(self, cam_id: str) -> Optional[tuple[int, int]]:
        """Return (width, height) of the latest inferred frame, or None."""
        return self._cam_resolution.get(cam_id)

    def set_iter_cpu_set(self, cpu_set: set[int]) -> None:
        """Pin the Voyager iter thread to ``cpu_set`` once it starts."""
        self._iter_cpu_set = set(cpu_set) if cpu_set else set()

    def set_viewer_check_fn(
        self, fn: Optional[Callable[[str], bool]],
    ) -> None:
        """Install a callback the iter thread uses to decide whether to
        memcpy a frame into SHM.

        Pass ``None`` to disable gating (always write). The callback runs
        on the iter thread once per kept frame, so it must be cheap and
        thread-safe.
        """
        self._viewer_check_fn = fn

    def _should_write_slot(self, cam_id: str, now: float) -> bool:
        fn = self._viewer_check_fn
        if fn is None:
            return True
        try:
            has_viewer = bool(fn(cam_id))
        except Exception:
            return True  # fail-open; never block frames on a buggy callback
        if has_viewer:
            self._last_viewer_seen_ts[cam_id] = now
            return True
        last = self._last_viewer_seen_ts.get(cam_id, 0.0)
        return (now - last) < self._viewer_grace_s

    # ------------------------------------------------------------------
    # Internal — pipeline plumbing
    # ------------------------------------------------------------------

    def _pick_pipeline_index(self) -> int:
        if not self._pipeline_source_counts:
            return -1
        candidates = [
            (count, idx)
            for idx, count in enumerate(self._pipeline_source_counts)
            if idx not in self._pipeline_broken and count < MAX_SOURCES_PER_PIPELINE
        ]
        if not candidates:
            return -1
        return min(candidates)[1]

    def _add_source(self, cam_id: str, source_url: str) -> None:
        try:
            from axelera.app.stream import create_inference_stream
            from axelera.app.config import (
                HardwareCaps, HardwareEnable, Source, SystemConfig,
            )
        except ImportError as e:
            logger.error("[Voyager] ImportError — is Voyager SDK venv active? %s", e)
            with self._lock:
                self._running[cam_id] = False
            return

        with self._deploy_lock:
            try:
                if self._stream is None:
                    logger.info("[Voyager] %s creating shared inference stream...", cam_id)
                    system_config = SystemConfig(
                        allow_hardware_codec=cfg.VOYAGER_ALLOW_HARDWARE_CODEC,
                        hardware_caps=HardwareCaps(
                            vaapi=(
                                HardwareEnable.enable
                                if cfg.VOYAGER_ENABLE_VAAPI
                                else HardwareEnable.disable
                            ),
                            opencl=HardwareEnable.detect,
                            opengl=HardwareEnable.detect,
                        ),
                    )
                    stream = create_inference_stream(
                        system_config=system_config,
                        network=self._network,
                        sources=[source_url],
                        pipe_type="gst",
                        aipu_cores=cfg.VOYAGER_AIPU_CORES,
                        low_latency=True,
                        specified_frame_rate=max(1, round(cfg.TARGET_FPS)),
                    )
                    pipe0 = stream._pipelines[0]
                    if not pipe0.sources:
                        raise RuntimeError("Pipeline created but no sources connected")
                    self._stream = stream
                    self._pipelines.append(pipe0)
                    self._pipeline_source_counts.append(1)
                    self._pipeline_broken.discard(0)
                    with self._lock:
                        for sid in pipe0.sources:
                            self._sid_to_cam[sid] = cam_id
                            self._cam_to_sid[cam_id] = sid
                    logger.info(
                        "[Voyager] %s stream ready (pipeline=0, network=%s)",
                        cam_id, self._network,
                    )
                    self._stream_thread = threading.Thread(
                        target=self._iteration_loop,
                        daemon=True,
                        name="Voyager-iter",
                    )
                    self._stream_thread.start()
                    return

                if len(self._pipelines) < self._max_pipelines:
                    idx = len(self._pipelines)
                    pipe_new = self._stream.add_pipeline(
                        sources=[source_url],
                        aipu_cores=cfg.VOYAGER_AIPU_CORES,
                        specified_frame_rate=max(1, round(cfg.TARGET_FPS)),
                    )
                    self._pipelines.append(pipe_new)
                    self._pipeline_source_counts.append(1)
                    self._pipeline_broken.discard(idx)
                    with self._lock:
                        for sid in pipe_new.sources:
                            self._sid_to_cam[sid] = cam_id
                            self._cam_to_sid[cam_id] = sid
                    logger.info(
                        "[Voyager] %s pipeline ready (pipeline=%d, network=%s)",
                        cam_id, idx, self._network,
                    )
                else:
                    idx = self._pick_pipeline_index()
                    if idx < 0:
                        logger.error(
                            "[Voyager] %s rejected — all %d pipelines full (%d each)",
                            cam_id, len(self._pipelines), MAX_SOURCES_PER_PIPELINE,
                        )
                        with self._lock:
                            self._running[cam_id] = False
                        return
                    sid = self._pipelines[idx].add_source(Source(source_url))
                    self._pipeline_source_counts[idx] += 1
                    with self._lock:
                        self._sid_to_cam[sid] = cam_id
                        self._cam_to_sid[cam_id] = sid
                        self._sid_to_pipeline_idx[sid] = idx
                        # Prime the per-pipeline timestamp so we don't trip
                        # the staleness watchdog before the first frame
                        # arrives from this pipeline.
                        self._pipeline_last_frame_ts.setdefault(idx, time.time())
                    logger.info(
                        "[Voyager] %s source added (pipeline=%d, stream_id=%d)",
                        cam_id, idx, sid,
                    )
            except Exception as e:
                logger.error("[Voyager] %s add_source failed: %s", cam_id, e)
                if (
                    "idx" in locals()
                    and isinstance(idx, int)
                    and 0 <= idx < len(self._pipelines)
                ):
                    self._pipeline_broken.add(idx)
                with self._lock:
                    self._running[cam_id] = False
                    err_cb = self._error_callbacks.get(cam_id)
                    self._callbacks.pop(cam_id, None)
                    self._error_callbacks.pop(cam_id, None)
                    self._dropped_callbacks.pop(cam_id, None)
                    self._drop_log_ts.pop(cam_id, None)
                if err_cb:
                    try:
                        err_cb()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # The hot loop
    # ------------------------------------------------------------------

    def _check_pipeline_staleness(self, now: float, stale_threshold_s: float) -> None:
        """Self-SIGKILL when a single Voyager pipeline has wedged.

        Symptom we're catching: the shared InferenceStream multiplexes
        results from all 4 pipelines into one iterator. If pipeline N
        stops producing while the others keep going, the SDK doesn't
        raise — frames for the wedged pipeline simply stop arriving.
        Every camera on that pipeline goes dark while the throughput
        log keeps growing from the healthy peers, so start.sh's
        aggregate watchdog can't see it. Observed three times today,
        always recovered by a manual edge restart.

        Decision rule:
          * Need at least 2 pipelines to compare (no peer = no wedge).
          * Need at least one peer to be *fresh* (within half the
            threshold). If no pipeline is fresh, the aggregate
            throughput watchdog will handle it; this routine stays
            quiet to avoid a self-kill loop on a full stall.
          * A pipeline is stale only if it owns at least one source
            (otherwise the timestamp is just frozen on an empty
            pipeline).

        Recovery: SIGKILL via the kernel (same path as the
        FATAL_STREAK_LIMIT branch above). The supervisor relaunches.
        """
        if len(self._pipeline_last_frame_ts) < 2:
            return
        ages = {pidx: now - ts for pidx, ts in self._pipeline_last_frame_ts.items()}
        freshest = min(ages.values())
        if freshest > stale_threshold_s / 2:
            return  # no fresh peer; let the aggregate watchdog handle it
        stale = []
        for pidx, age in ages.items():
            if age <= stale_threshold_s:
                continue
            if pidx >= len(self._pipeline_source_counts):
                continue
            if self._pipeline_source_counts[pidx] <= 0:
                continue
            stale.append((pidx, age))
        if not stale:
            return
        detail = ", ".join(
            f"pipeline={pidx} age={age:.0f}s sources={self._pipeline_source_counts[pidx]}"
            for pidx, age in stale
        )
        logger.critical(
            "[Voyager] single-pipeline wedge detected (%s); peers fresh "
            "(freshest=%.1fs); killing process so the supervisor restarts",
            detail, freshest,
        )
        os.kill(os.getpid(), signal.SIGKILL)

    def _check_camera_staleness(self, now: float, stale_threshold_s: float) -> None:
        """Trigger per-camera RTSP recovery when a single source goes silent
        while its pipeline peers stay healthy.

        Symptom we're catching: the Voyager SDK emits
        ``FrameEvent(source_error, source_id=N, …)`` when an RTSP source
        loses its read, but in production we've seen the SDK log
        ``Ignoring event …`` and never invoke our error_callback. The
        source stays attached, no frames arrive, the camera's dwell
        clock freezes, and operators see the camera marked errored
        until a manual delete/recreate. Mirrors the per-pipeline
        watchdog but at camera scope.

        Decision rule:
          * The camera's last frame is older than ``stale_threshold_s``.
          * Its pipeline is fresh (peer cameras on the same pipeline
            are still producing frames) — otherwise the per-pipeline
            watchdog will handle the broader wedge and a per-camera
            recovery here would race with that.
          * The camera hasn't already had a recovery attempt in the
            last 60s (dedupe; cleared on the first fresh frame).

        Recovery: invoke the camera's error_callback, which is
        ``CameraStream.mark_errored`` — detaches the dead source, kicks
        the existing retry thread that preflights the URL and
        ``add_stream``s a fresh source.
        """
        if not self._last_cb_ts:
            return
        recovery_cooldown_s = 60.0
        for cam_id, last_cb in list(self._last_cb_ts.items()):
            if (now - last_cb) <= stale_threshold_s:
                continue
            sid = self._cam_to_sid.get(cam_id)
            if sid is None:
                continue
            pidx = self._sid_to_pipeline_idx.get(sid)
            if pidx is None:
                continue
            pipeline_ts = self._pipeline_last_frame_ts.get(pidx, 0.0)
            if pipeline_ts <= 0 or (now - pipeline_ts) > stale_threshold_s / 2:
                continue  # pipeline-level issue; let _check_pipeline_staleness handle it
            last_attempt = self._last_camera_recovery_ts.get(cam_id, 0.0)
            if (now - last_attempt) < recovery_cooldown_s:
                continue
            cb = self._error_callbacks.get(cam_id)
            if cb is None:
                continue
            self._last_camera_recovery_ts[cam_id] = now
            logger.warning(
                "[Voyager] camera %s silent %.0fs (pipeline=%d fresh); "
                "invoking source-error recovery",
                cam_id, now - last_cb, pidx,
            )
            try:
                cb()
            except Exception:
                logger.exception(
                    "[Voyager] %s error_callback raised during recovery",
                    cam_id,
                )

    def _iteration_loop(self) -> None:
        """Pull frames from the shared InferenceStream.

        Designed to never throw out of the loop body — any exception on
        a single frame is logged and the iterator continues so 30
        cameras don't go dark because of one bad frame.

        Exceptions to that:

          * Drained stream — every source has detached and the SDK raises
            ``ValueError: No pipeline configs provided`` (whole camera
            network dropped at once). We do NOT kill the process: a restart
            just finds the same unreachable cameras and crash-loops. Instead
            ``_recover_stream_to_idle`` tears the stream down and re-arms the
            cameras for background retry, then this thread exits; the stream
            rebuilds lazily once a source is reachable again.

          * True SDK wedge — ``InferenceStream terminated`` /
            ``'NoneType' object is not iterable`` repeating, or a drained
            stream whose ``stop()`` itself hangs. After ``FATAL_STREAK_LIMIT``
            consecutive fatals we SIGKILL so ``start.sh`` restarts from
            scratch (catching-and-continuing would just spam at ~2 Hz while
            ``cams_active`` sits at 0).
        """
        if self._iter_cpu_set:
            from edge_agent.pipeline.cpu_topology import pin_current_thread
            if pin_current_thread(self._iter_cpu_set):
                logger.info(
                    "[Voyager] iter thread pinned to CPU %s",
                    sorted(self._iter_cpu_set),
                )
        registry = self._frame_registry
        fatal_streak = 0
        FATAL_MARKERS = (
            "No pipeline configs provided",
            "InferenceStream terminated",
            "'NoneType' object is not iterable",
        )
        # Bumped 5→15. With FATAL_BACKOFF_S=2 (below), this gives the SDK
        # ~30 s to self-recover before we kill the process, vs the old
        # 2.5 s window which was killing on flurries that often cleared
        # on their own. True permanent wedges still get caught — they
        # just take 30 s longer to confirm.
        FATAL_STREAK_LIMIT = 15
        FATAL_BACKOFF_S = 2.0
        # Per-pipeline staleness watchdog. Healthy pipelines emit a frame
        # roughly every 30 ms; PIPELINE_STALE_S=180 means a pipeline is
        # "wedged" only when it's been silent for 3 minutes while a peer
        # is fresh. The check itself is cheap so we run it every 5 s.
        # Raised from 60→180 because a brief upstream switch event
        # (cameras drop ping for 30-90 s and come back) was triggering
        # self-SIGKILL even though the cameras recovered on their own
        # within the old threshold + a few seconds. 180 s tolerates
        # typical transient network blips while still catching a true
        # SDK wedge within a few minutes.
        PIPELINE_STALE_S = 180.0
        # Per-camera staleness — well above normal jitter (~30 ms between
        # frames on a healthy 30-fps source) but short enough that an
        # operator sees an autorecovered camera within a minute or so.
        CAMERA_STALE_S = 30.0
        WATCHDOG_INTERVAL_S = 5.0
        last_watchdog_check = time.time()
        while True:
            try:
                stream = self._stream
                if stream is None:
                    return
                for frame_result in stream:
                    self._handle_frame_result(frame_result, registry)
                    # A successful iteration means the SDK is healthy
                    # again; reset the streak so a later transient
                    # blip doesn't tip us over the threshold.
                    if fatal_streak:
                        fatal_streak = 0
                    now = time.time()
                    if now - last_watchdog_check > WATCHDOG_INTERVAL_S:
                        last_watchdog_check = now
                        self._check_pipeline_staleness(now, PIPELINE_STALE_S)
                        self._check_camera_staleness(now, CAMERA_STALE_S)
            except StopIteration:
                logger.info("[Voyager] iteration stream exhausted")
                return
            except Exception as e:
                logger.exception("[Voyager] iteration loop error; continuing")
                msg = str(e)
                # Every source has drained from the stream — in production
                # this means the whole camera network dropped at once (the
                # 8 pm outage). SIGKILLing the process here is futile: the
                # supervisor restarts, the fresh agent finds the same
                # unreachable cameras, the SDK can't build a pipeline, and
                # it crash-loops for hours. Instead tear the dead stream
                # down to idle and let each CameraStream's background
                # preflight-retry rebuild it once its source is reachable
                # again (_add_source recreates the stream + iter thread).
                # The process — and the cloud link — stay up, and recovery
                # is automatic when the network returns. Only fall through
                # to the SIGKILL path if the teardown can't complete, so a
                # genuinely wedged SDK still gets reset.
                if "No pipeline configs provided" in msg:
                    if self._recover_stream_to_idle():
                        return
                if any(marker in msg for marker in FATAL_MARKERS):
                    fatal_streak += 1
                else:
                    fatal_streak = 0
                if fatal_streak >= FATAL_STREAK_LIMIT:
                    logger.critical(
                        "[Voyager] SDK reports stream is permanently dead "
                        "(%d consecutive fatal iterations); killing process "
                        "so the supervisor restarts the agent",
                        fatal_streak,
                    )
                    # SIGKILL via the kernel — uncatchable, unblockable, and
                    # does not depend on the Python interpreter or GIL being
                    # responsive. os._exit was observed to hang here (the SDK
                    # holds C-side locks during its own deadlock), leaving
                    # the process in futex_wait_queue with orphan workers
                    # still pinning /dev/metis*. The start.sh supervisor
                    # then reaps any survivors via pkill + fuser.
                    os.kill(os.getpid(), signal.SIGKILL)
                time.sleep(FATAL_BACKOFF_S)

    def _recover_stream_to_idle(self) -> bool:
        """Tear a fully-drained InferenceStream down to idle (no restart).

        Called from the iter loop when the SDK raises "No pipeline configs
        provided" — every source has detached, typically because the whole
        camera network dropped at once. Rather than SIGKILL the process
        (which only crash-loops against the same unreachable cameras), we:

          1. Stop the dead stream and drop all pipeline bookkeeping, so the
             next ``_add_source`` recreates it from scratch.
          2. Re-arm every still-registered camera via its error callback
             (``CameraStream.mark_errored``), which detaches it and starts
             the background preflight-retry. When a source becomes reachable
             again the retry calls ``add_stream`` → ``_add_source``, which
             recreates the stream and starts a fresh iter thread.

        The current iter thread then exits (caller ``return``s). If a source
        is reachable the stream rebuilds within seconds; if none are, the
        agent sits cleanly idle (throughput heartbeats keep the start.sh
        watchdog satisfied) until the network returns.

        Returns True if teardown completed and the iter thread should exit;
        False if ``stream.stop()`` hung, so the caller falls back to the
        SIGKILL path and a genuinely wedged SDK still gets reset.
        """
        # Bound stream.stop(): a wedged SDK can hold C-side locks and hang
        # here forever. If it doesn't return promptly, report failure and
        # let the caller SIGKILL — exactly the old behavior for a true wedge.
        stream = self._stream
        if stream is not None:
            stopped = threading.Event()

            def _stop() -> None:
                try:
                    stream.stop()
                except Exception as exc:
                    logger.warning(
                        "[Voyager] stream.stop() during idle recovery failed: %s",
                        exc,
                    )
                finally:
                    stopped.set()

            threading.Thread(
                target=_stop, daemon=True, name="Voyager-idle-stop",
            ).start()
            if not stopped.wait(timeout=10.0):
                logger.error(
                    "[Voyager] stream.stop() hung during idle recovery; "
                    "falling back to process restart",
                )
                return False

        with self._deploy_lock:
            self._stream = None
            self._stream_thread = None
            self._pipelines.clear()
            self._pipeline_source_counts.clear()
            self._pipeline_broken.clear()

        with self._lock:
            self._sid_to_pipeline_idx.clear()
            self._pipeline_last_frame_ts.clear()
            cams = [c for c, running in self._running.items() if running]
            err_cbs = {c: self._error_callbacks.get(c) for c in cams}
            self._sid_to_cam.clear()
            self._cam_to_sid.clear()

        logger.warning(
            "[Voyager] all sources gone; stream torn down to idle and "
            "%d camera(s) re-armed for background retry (no process restart)",
            len(cams),
        )
        # Invoke error callbacks OUTSIDE the engine locks — mark_errored
        # re-enters the engine via remove_stream (and CameraStream's own
        # locks), so holding _lock/_deploy_lock here would deadlock.
        for cam_id in cams:
            cb = err_cbs.get(cam_id)
            if cb is None:
                continue
            try:
                cb()
            except Exception as exc:
                logger.warning(
                    "[Voyager] re-arm callback for %s failed: %s", cam_id, exc,
                )
        return True

    def _handle_frame_result(self, frame_result, registry) -> None:
        # Count every result Metis returns, before any Python-side filtering.
        self._total_frames_inferenced += 1

        sid = frame_result.stream_id
        cam_id = self._sid_to_cam.get(sid)
        if not cam_id or not self._running.get(cam_id, False):
            return

        now = time.time()
        # Per-pipeline liveness: track the most recent frame timestamp for
        # whichever pipeline this stream lives on. _check_pipeline_staleness
        # compares these to detect a single-pipeline wedge.
        pidx = self._sid_to_pipeline_idx.get(sid)
        if pidx is not None:
            self._pipeline_last_frame_ts[pidx] = now
        # Camera is producing frames again — clear any dedupe entry so a
        # *future* stall on the same camera can fire mark_errored again
        # instead of being suppressed by the prior recovery's cooldown.
        self._last_camera_recovery_ts.pop(cam_id, None)
        # Per-camera frame-rate gate disabled: Metis's specified_frame_rate
        # already caps producer rate, the shards have ~95% headroom to
        # absorb the SDK's jitter, and the gate was dropping ~27% of
        # frames in steady state. _last_cb_ts is kept updated so a future
        # smarter gate (sliding window, scheduled-from-last-accepted) can
        # be reintroduced without restoring this code path.
        self._last_cb_ts[cam_id] = now

        t0 = time.perf_counter()

        # Extract dimensions directly from the SDK image to avoid Numpy conversion
        # on the iteration thread when no viewer is active.
        h = getattr(frame_result.image, "height", 1080)
        w = getattr(frame_result.image, "width", 1920)
        self._cam_resolution[cam_id] = (w, h)

        # Push frame to per-camera SHM (one memcpy; no pickling). Skip
        # entirely when no viewer is consuming the preview path; the
        # snapshot HTTP endpoint will fall back to the lazy OpenCV
        # decoder for cold reads.
        if registry is not None and self._should_write_slot(cam_id, now):
            try:
                path = self._asarray_path.get(cam_id)
                if path is None:
                    frame_np, path = _probe_asarray_path(frame_result.image)
                    self._asarray_path[cam_id] = path
                else:
                    frame_np = _as_bgr(frame_result.image, path)
                if frame_np is not None:
                    slot = registry.get(cam_id)
                    if slot is not None:
                        slot.write(frame_np, now)
            except Exception:
                pass

        # Detection extract — Voyager already does NMS, so we trust the list.
        # Hot path: one bulk pass to materialise an (N, 6) float32 array,
        # then a single vectorised numpy mask for class / conf / area /
        # aspect. The previous per-detection Python loop was the dominant
        # source of GIL pressure on this thread (one P-core pegged at ~95%
        # with N cameras × M dets/frame at TARGET_FPS).
        try:
            det_list = frame_result.detections
            n_in = len(det_list) if det_list is not None else 0
            if n_in == 0:
                detections = np.empty((0, 6), dtype=np.float32)
            else:
                raw = np.empty((n_in, 6), dtype=np.float32)
                for i, det in enumerate(det_list):
                    b = det.box
                    raw[i, 0] = b[0]
                    raw[i, 1] = b[1]
                    raw[i, 2] = b[2]
                    raw[i, 3] = b[3]
                    raw[i, 4] = det.score
                    raw[i, 5] = det.class_id

                bw = raw[:, 2] - raw[:, 0]
                bh = raw[:, 3] - raw[:, 1]
                area = bw * bh
                aspect = bw / np.maximum(bh, 1.0)

                cls_ids = raw[:, 5].astype(np.int32, copy=False)
                lut = self._vehicle_class_lut
                if lut.size:
                    in_bounds = (cls_ids >= 0) & (cls_ids < lut.size)
                    cls_ok = np.zeros(n_in, dtype=bool)
                    cls_ok[in_bounds] = lut[cls_ids[in_bounds]]
                else:
                    cls_ok = np.zeros(n_in, dtype=bool)

                keep = (
                    cls_ok
                    & (raw[:, 4] >= self._conf)
                    & (bw > 0) & (bh > 0)
                    & (area >= MIN_BBOX_AREA) & (area <= MAX_BBOX_AREA)
                    & (aspect >= MIN_ASPECT) & (aspect <= MAX_ASPECT)
                )
                detections = raw[keep]
        except Exception:
            logger.exception("[Voyager] %s detection extract error", cam_id)
            detections = np.empty((0, 6), dtype=np.float32)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # Bounded deque is thread-safe for append; avoid global lock.
        self._inf_times.append(elapsed_ms)

        # Per-camera FPS bookkeeping (single writer = this thread, lock-free).
        cnt = self._cam_frame_counts.get(cam_id, 0) + 1
        self._cam_frame_counts[cam_id] = cnt
        t0_fps = self._cam_fps_t0.get(cam_id, now)
        if now - t0_fps >= 1.0:
            self._cam_fps[cam_id] = round(cnt / (now - t0_fps), 1)
            self._cam_frame_counts[cam_id] = 0
            self._cam_fps_t0[cam_id] = now

        cb = self._callbacks.get(cam_id)
        if cb is None:
            return
        avg_ms = self.avg_inf_ms
        cur_fps = self._cam_fps.get(cam_id, 0.0)
        try:
            cb(detections, w, h, now, avg_ms, cur_fps)
        except Exception:
            logger.exception("[Voyager] %s callback error", cam_id)
