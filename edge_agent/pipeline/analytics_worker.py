"""Sharded analytics workers — vehicle tracking and zone counting.

Replaces the single-process AnalyticsWorker with a process pool keyed by
``hash(camera_id) % K``. Each shard owns a slice of cameras end-to-end:
tracker state, zone analytics, and MQTT publishing for that slice never
leave the process.

Crucially, the workers no longer receive frame pixels — only
``(cam_id, dets, frame_w, frame_h, ts, inf_ms, fps)``. This collapses the
per-frame IPC payload from ~6 MB to a few hundred bytes.

The frame pixels (when needed for the live preview) flow through a
separate SharedMemory channel directly to the UIRenderer.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import time

from edge_agent import config as cfg
from edge_agent.comms.mqtt_publisher import MQTTPublisher
from edge_agent.pipeline.ocsort import OCSORTTracker
from edge_agent.pipeline.vehicle_analytics import VehicleZoneAnalytics

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Worker process entry point
# --------------------------------------------------------------------------

def _analytics_shard_proc(
    shard_idx: int,
    num_shards: int,
    task_queue: "multiprocessing.Queue",
    ui_signal_queue: "multiprocessing.Queue",
    viewer_array,  # multiprocessing.Array of int counts, indexed by cam slot
    cam_slot_map: dict,  # camera_id -> int slot index (snapshot at startup)
    cpu_set: set,  # CPU IDs to pin this shard to (empty = unpinned)
    det_ring_view,  # DetectionRingView; reads bulk detections lock-free
    frames_tracked_array,  # multiprocessing.Array("Q", num_shards, lock=False)
    poll_idle_sleep_s: float = 0.001,
):
    """Entry point for one analytics shard.

    The shard owns a subset of cameras. It runs tracking + zone analytics
    + MQTT publishing for those cameras only. ``ui_signal_queue`` is used
    to wake the UI worker when a watched camera produces a new frame.

    Data path is queue-less: the shard polls the SHM detection ring for
    its assigned cameras. ``task_queue`` carries control messages only
    (CONFIG, REMOVE, and a ``None`` sentinel to stop).
    """
    try:
        os.nice(5)
    except Exception:
        pass

    # Each shard is pinned to a single core. OpenCV defaults its internal
    # thread pool to the host's logical CPU count (16 here). Those workers
    # all land on the one core we are pinned to and fight for the GIL,
    # producing ~80M nonvoluntary context switches per shard observed in
    # /proc/<tid>/status. Disabling the cv2 pool before any cv2 call (cv2
    # is pulled in transitively by VehicleZoneAnalytics) reclaims that
    # core for the polling loop and the tracker.
    try:
        import cv2  # noqa: F401  (imported for the side effect of being importable)
        cv2.setNumThreads(0)
    except Exception:
        pass

    if cpu_set:
        from edge_agent.pipeline.cpu_topology import pin_current_thread
        if pin_current_thread(set(cpu_set)):
            logging.getLogger(__name__).info(
                "AnalyticsShard %d pinned to CPU %s", shard_idx, sorted(cpu_set),
            )

    proc_logger = logging.getLogger(f"analytics-{shard_idx}")
    proc_logger.info("AnalyticsShard %d started (pid=%d)", shard_idx, os.getpid())

    mqtt_pub = MQTTPublisher()
    try:
        mqtt_pub.connect()
    except Exception:
        proc_logger.exception("Shard %d: MQTT connect failed", shard_idx)

    # Per-shard Redis client used by VehicleZoneAnalytics to persist zone-entry
    # timestamps across edge restarts. Created lazily so an import failure or
    # connection refusal doesn't crash the shard — VehicleZoneAnalytics treats
    # `redis_client=None` as "fall back to pure in-memory dwell tracking" and
    # logs once if a live client later errors out.
    redis_client = None
    try:
        import redis as _redis_lib
        redis_client = _redis_lib.Redis.from_url(
            cfg.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        redis_client.ping()
        proc_logger.info("Shard %d: Redis connected (%s)", shard_idx, cfg.REDIS_URL)
    except Exception as e:
        proc_logger.warning(
            "Shard %d: Redis unavailable (%s) — dwell will reset on edge restart",
            shard_idx, e,
        )
        redis_client = None

    trackers: dict[str, OCSORTTracker] = {}
    analytics: dict[str, dict[str, VehicleZoneAnalytics]] = {}
    last_publish_ts: dict[str, dict[str, float]] = {}
    last_ui_signal_ts: dict[str, float] = {}
    # Per-camera read index into the multi-slot detection ring. The
    # consumer advances by one sub-slot per DATA token (Option i FIFO),
    # so each inferenced frame produces exactly one tracker.update call.
    last_read_idx: dict[str, int] = {}

    # Local copy of cam→slot map filtered to cameras this shard owns.
    # The producer-side route is ``hash(cam_id) % num_shards``; we apply
    # the same filter to the inherited snapshot so polling only touches
    # ring slots we are responsible for. CONFIG messages adding/removing
    # cameras at runtime are routed only to the owning shard already,
    # so updates via the queue do not need to re-check ownership.
    cam_slots: dict[str, int] = {
        cid: slot for cid, slot in cam_slot_map.items()
        if hash(cid) % num_shards == shard_idx
    }

    def _ensure_cam(cam_id: str) -> None:
        if cam_id not in trackers:
            trackers[cam_id] = OCSORTTracker(
                high_thresh=cfg.OCSORT_HIGH_THRESH,
                low_thresh=cfg.OCSORT_LOW_THRESH,
                match_thresh=cfg.OCSORT_MATCH_THRESH,
                max_time_lost=cfg.OCSORT_MAX_TIME_LOST,
                bbox_inflation=cfg.OCSORT_BBOX_INFLATION,
            )
            analytics.setdefault(cam_id, {})
            last_publish_ts.setdefault(cam_id, {})

    def _has_viewer(cam_id: str) -> bool:
        slot = cam_slots.get(cam_id)
        if slot is None:
            return False
        try:
            return viewer_array[slot] > 0
        except Exception:
            return False

    stop_requested = False
    while not stop_requested:
        # 1. Drain control messages without blocking. CONFIG/REMOVE are
        # low-frequency so they never back up; ``None`` is the shutdown
        # sentinel. DATA tokens no longer arrive on this queue.
        while True:
            try:
                task = task_queue.get_nowait()
            except queue.Empty:
                break
            if task is None:
                stop_requested = True
                break
            try:
                kind = task[0]
                if kind == "CONFIG":
                    _, cam_id, slot_idx, zones = task
                    cam_slots[cam_id] = slot_idx
                    _ensure_cam(cam_id)
                    _sync_zones(cam_id, zones, analytics, redis_client)
                elif kind == "REMOVE":
                    _, cam_id = task
                    trackers.pop(cam_id, None)
                    analytics.pop(cam_id, None)
                    last_publish_ts.pop(cam_id, None)
                    last_ui_signal_ts.pop(cam_id, None)
                    last_read_idx.pop(cam_id, None)
                    cam_slots.pop(cam_id, None)
                # Any other kind is ignored — DATA tokens are obsolete.
            except Exception:
                proc_logger.exception("Shard %d control error", shard_idx)
        if stop_requested:
            break

        # 2. Poll the SHM ring for every camera this shard owns. One
        # frame per cam per pass keeps fairness across cameras; if a cam
        # falls more than ring_depth frames behind, ``read_one`` jumps
        # forward to the oldest still-valid sub-slot.
        drained_any = False
        for cam_id, slot in list(cam_slots.items()):
            result = det_ring_view.read_one(
                slot, last_read_idx.get(cam_id, 0),
            )
            if result is None:
                continue
            new_read_idx, det_arr, frame_w, frame_h, ts, inf_ms, fps = result
            last_read_idx[cam_id] = new_read_idx
            drained_any = True

            try:
                detections = [
                    (float(r[0]), float(r[1]), float(r[2]),
                     float(r[3]), float(r[4]), int(r[5]))
                    for r in det_arr
                ]

                _ensure_cam(cam_id)

                tracker = trackers[cam_id]
                tracked = tracker.update(detections)
                frames_tracked_array[shard_idx] += 1

                cam_zones = analytics.get(cam_id, {})
                cam_pub_ts = last_publish_ts.setdefault(cam_id, {})

                for zid, zone_obj in cam_zones.items():
                    last_ts = cam_pub_ts.get(zid, 0.0)
                    if ts - last_ts < 1.0:
                        continue
                    metrics = zone_obj.update(
                        tracked, frame_w, frame_h, ts, inf_ms, fps,
                    )
                    mqtt_pub.publish_metrics(metrics)
                    for alert in zone_obj.check_alerts(metrics, ts):
                        mqtt_pub.publish_alert(alert)
                    cam_pub_ts[zid] = ts

                # Wake the UI worker only when:
                #   - someone is watching this camera, AND
                #   - we haven't already signalled within the live-preview interval.
                if _has_viewer(cam_id):
                    live_fps = max(0.1, float(getattr(cfg, "LIVE_ANNOTATE_FPS", 2.0)))
                    interval = 1.0 / live_fps
                    if ts - last_ui_signal_ts.get(cam_id, 0.0) >= interval:
                        last_ui_signal_ts[cam_id] = ts

                        # Each polygon carries its display name so the
                        # renderer can stamp "Zone 5" at the polygon's
                        # centroid in the zone's palette colour. zone_label
                        # comes from the operator-facing name with a
                        # fallback to zone_id when name is empty.
                        zone_polys: list[tuple[str, list, str]] = []
                        # Per-track (dwell_secs, threshold_secs). Threshold is
                        # the dwell-zone's max_dwell_time_s so the UI renderer
                        # can color the bbox green/yellow/red without knowing
                        # zone config. When a track sits in multiple zones we
                        # keep the longest dwell and carry that zone's threshold.
                        track_dwell: dict[int, tuple[float, float]] = {}
                        for zid, zone_obj in cam_zones.items():
                            poly = zone_obj.zone_poly_canvas
                            zone_label = (
                                getattr(zone_obj._cfg, "name", None) or zid
                            )
                            if poly is not None:
                                zone_polys.append((zid, poly.tolist(), zone_label))
                            threshold = float(
                                getattr(zone_obj._cfg, "max_dwell_time_s", 0.0) or 0.0
                            )
                            for tid, entry_ts in zone_obj._zone_entry.items():
                                d = ts - entry_ts
                                cur = track_dwell.get(tid)
                                if cur is None or d > cur[0]:
                                    track_dwell[tid] = (d, threshold)

                        try:
                            ui_signal_queue.put_nowait((
                                cam_id,
                                tracked,
                                zone_polys,
                                (frame_w, frame_h),
                                (inf_ms, fps),
                                ts,
                                track_dwell,
                            ))
                        except queue.Full:
                            pass
            except Exception:
                proc_logger.exception("Shard %d frame error", shard_idx)

        if not drained_any:
            time.sleep(poll_idle_sleep_s)

    try:
        mqtt_pub.disconnect()
    except Exception:
        pass
    proc_logger.info("AnalyticsShard %d exiting", shard_idx)


def _sync_zones(cam_id: str, zones, analytics_map: dict, redis_client=None) -> None:
    current = analytics_map.setdefault(cam_id, {})
    new_ids = {z.zone_id for z in zones}
    for z_cfg in zones:
        if z_cfg.zone_id in current:
            current[z_cfg.zone_id].update_config(z_cfg)
        else:
            current[z_cfg.zone_id] = VehicleZoneAnalytics(
                z_cfg, cfg.EDGE_ID, redis_client=redis_client,
            )
    for zid in list(current.keys()):
        if zid not in new_ids:
            del current[zid]


# --------------------------------------------------------------------------
# Pool manager
# --------------------------------------------------------------------------

class AnalyticsPool:
    """Manages a fleet of analytics shard processes.

    Cameras are sharded by ``hash(cam_id) % num_shards``. Each shard owns
    its slice of cameras end-to-end (tracker, zones, MQTT). The producer
    side calls submit_data / update_config / remove_camera and never
    needs to know which shard a camera lives on.
    """

    def __init__(
        self,
        num_shards: int,
        ui_signal_queue,
        viewer_array,
        cpu_layout=None,
        det_ring_view=None,
    ):
        self._num_shards = max(1, int(num_shards))
        self._ui_signal_queue = ui_signal_queue
        self._viewer_array = viewer_array
        self._cpu_layout = cpu_layout
        self._det_ring_view = det_ring_view
        self._task_queues: list[multiprocessing.Queue] = [
            multiprocessing.Queue(maxsize=200) for _ in range(self._num_shards)
        ]
        self._processes: list[multiprocessing.Process] = []
        self._cam_slot_map: dict[str, int] = {}
        # Per-shard tracked-frame counters. Single writer per slot,
        # main process aggregates via total_frames_tracked().
        self._frames_tracked_array = multiprocessing.Array(
            "Q", self._num_shards, lock=False,
        )
        self._started = False

    def total_frames_tracked(self) -> int:
        return sum(self._frames_tracked_array[i] for i in range(self._num_shards))

    @property
    def num_shards(self) -> int:
        return self._num_shards

    def _shard_for(self, cam_id: str) -> int:
        return hash(cam_id) % self._num_shards

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for idx in range(self._num_shards):
            cpu_set = (
                self._cpu_layout.shard_set(idx)
                if self._cpu_layout is not None else set()
            )
            p = multiprocessing.Process(
                target=_analytics_shard_proc,
                args=(
                    idx,
                    self._num_shards,
                    self._task_queues[idx],
                    self._ui_signal_queue,
                    self._viewer_array,
                    self._cam_slot_map,
                    cpu_set,
                    self._det_ring_view,
                    self._frames_tracked_array,
                ),
                daemon=True,
                name=f"AnalyticsShard-{idx}",
            )
            p.start()
            self._processes.append(p)
        logger.info(
            "AnalyticsPool started with %d shards", self._num_shards,
        )

    def stop(self) -> None:
        for q in self._task_queues:
            try:
                q.put_nowait(None)
            except Exception:
                pass
        for p in self._processes:
            try:
                p.join(timeout=2.0)
            except Exception:
                pass
            if p.is_alive():
                p.terminate()
        self._processes.clear()
        self._started = False

    def register_camera(self, cam_id: str, slot_idx: int) -> None:
        self._cam_slot_map[cam_id] = slot_idx

    def update_config(self, cam_id: str, slot_idx: int, zones) -> None:
        self.register_camera(cam_id, slot_idx)
        q = self._task_queues[self._shard_for(cam_id)]
        try:
            q.put_nowait(("CONFIG", cam_id, slot_idx, zones))
        except queue.Full:
            logger.warning("Analytics shard %d full, dropping CONFIG for %s",
                           self._shard_for(cam_id), cam_id)

    def remove_camera(self, cam_id: str) -> None:
        q = self._task_queues[self._shard_for(cam_id)]
        try:
            q.put_nowait(("REMOVE", cam_id))
        except queue.Full:
            pass
        self._cam_slot_map.pop(cam_id, None)

    def submit_data(
        self,
        cam_id: str,
        detections: list,
        frame_w: int,
        frame_h: int,
        ts: float,
        inf_ms: float,
        fps: float,
    ) -> None:
        # Single SHM write — no pickle, no kernel pipe, no GIL-blocking
        # syscall. The owning shard discovers the new frame by polling
        # ``write_idx`` for this slot in its ring view.
        slot = self._cam_slot_map.get(cam_id)
        if slot is not None and self._det_ring_view is not None:
            self._det_ring_view.write(
                slot, detections, frame_w, frame_h, ts, inf_ms, fps,
            )
