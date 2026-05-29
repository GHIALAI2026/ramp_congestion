"""UIRenderer — SharedMemory-driven annotated preview encoder.

The UI worker:
  * Reads the latest frame for a camera from a SharedMemory FrameSlot.
  * Draws a single cached zone overlay + per-detection bboxes/labels.
  * Encodes via TurboJPEG when available, else cv2.imencode.
  * Writes the JPEG bytes to a SharedMemory JPEGSlot.

It receives small "wake" signals from the analytics shards — never frame
pixels — so the queue payload is tiny and the queue depth (10) is more
than enough to absorb bursts.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import time
from typing import Optional

import cv2
import numpy as np

from edge_agent import config as cfg
from edge_agent.pipeline.shm_buffer import FrameSlot, JPEGSlot

logger = logging.getLogger(__name__)


# Bbox color is dwell-status driven (not per-class) — operators care more
# about "this car has been here too long" than about car-vs-truck, which is
# already in the label text. (BGR.)
COLOR_NORMAL = (60, 200, 60)     # green
COLOR_WARNING = (0, 220, 255)    # yellow
COLOR_OVERSTAY = (40, 40, 240)   # red

# Fractions of the zone's max_dwell_time_s at which the bbox flips color.
DWELL_WARN_RATIO = 0.5   # >=50% of threshold → yellow
DWELL_OVERSTAY_RATIO = 1.0

ZONE_ALPHA = 0.15
ZONE_EDGE_CONTRAST = (0, 0, 0)
# Palette indexed by zone position (BGR). Cameras with multiple zones — e.g.
# a single feed split before/after a speed breaker — get visually distinct
# polygons so the operator can tell at a glance which patch of the frame
# belongs to which zone. Each entry is (fill_bgr, edge_bgr); the edge color
# is just the fill color slightly punched-up for stroke visibility. Wraps
# around for cameras with > len(palette) zones.
ZONE_PALETTE = [
    ((255, 200,  60), (255, 220,  80)),  # cyan-blue
    ((110, 220, 110), (130, 240, 130)),  # green
    ((100, 100, 230), (120, 120, 255)),  # red-coral
    ((220, 130, 220), (240, 150, 240)),  # magenta
    ((60,  200, 255), (80,  220, 255)),  # amber
    ((220, 220, 100), (240, 240, 120)),  # cyan
]
# Edge thickness is computed per-frame from frame size so the line looks the
# same visual width across resolutions (see _edge_thickness).
ZONE_EDGE_REF_DIM = 1080  # reference short-side; below this, thickness clamps to 1px
ZONE_EDGE_BASE_PX = 2     # target px on a 1080p frame


# --------------------------------------------------------------------------
# Encoder abstraction — TurboJPEG with OpenCV fallback
# --------------------------------------------------------------------------

class _JPEGEncoder:
    def __init__(self, quality: int = 70):
        self._quality = quality
        self._tj = None
        if cfg.USE_TURBOJPEG:
            try:
                from turbojpeg import TurboJPEG, TJPF_BGR  # type: ignore
                self._tj = TurboJPEG()
                self._tj_pixel_format = TJPF_BGR
                logger.info("UIRenderer: using TurboJPEG (quality=%d)", quality)
            except Exception as e:
                logger.info(
                    "UIRenderer: TurboJPEG unavailable (%s); using cv2.imencode",
                    e,
                )

    def encode(self, frame: np.ndarray) -> Optional[bytes]:
        if self._tj is not None:
            try:
                return self._tj.encode(
                    frame,
                    quality=self._quality,
                    pixel_format=self._tj_pixel_format,
                )
            except Exception:
                logger.exception("TurboJPEG encode failed; falling back to cv2")
                self._tj = None  # don't keep retrying
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._quality]
        )
        if not ok:
            return None
        return buf.tobytes()


# --------------------------------------------------------------------------
# Drawing helpers — overlay built once per zone-config change
# --------------------------------------------------------------------------

class _ZoneOverlayCache:
    """One pre-rendered zone overlay (BGR + alpha mask) per (cam, frame_size)."""

    def __init__(self):
        # cam_id -> (zones_signature, frame_w, frame_h, overlay_bgr)
        self._cache: dict[str, tuple] = {}

    def get(
        self,
        cam_id: str,
        zones_canvas: list,
        frame_w: int,
        frame_h: int,
    ) -> tuple[Optional[np.ndarray], list]:
        """Returns (overlay, edges) where edges is a list of
        (edge_bgr, poly_int, label_or_None, centroid_or_None) tuples.
        The drawer paints each polygon in its zone-specific palette
        colour and stamps the zone name at the polygon's centroid so
        operators can read which zone is which without referencing the
        config table."""
        # Accept either (zid, poly) — legacy — or (zid, poly, name).
        # Length-based signature still works because the inner len(p)
        # depends only on the polygon points list.
        sig = tuple((entry[0], len(entry[1])) for entry in zones_canvas)
        cached = self._cache.get(cam_id)
        if cached is not None:
            cached_sig, cw, ch, overlay, edges = cached
            if cached_sig == sig and cw == frame_w and ch == frame_h:
                return overlay, edges

        if not zones_canvas:
            self._cache[cam_id] = (sig, frame_w, frame_h, None, [])
            return None, []

        sx = frame_w / cfg.CANVAS_W
        sy = frame_h / cfg.CANVAS_H
        overlay = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)
        edges: list = []
        for idx, entry in enumerate(zones_canvas):
            zid, poly = entry[0], entry[1]
            # 3rd element is the zone's display name when present;
            # fall back to zone_id for older signals.
            label = entry[2] if len(entry) > 2 and entry[2] else str(zid)
            if not poly:
                continue
            fill_bgr, edge_bgr = ZONE_PALETTE[idx % len(ZONE_PALETTE)]
            arr = np.array(poly, dtype=np.float32).reshape(-1, 1, 2)
            arr[:, 0, 0] *= sx
            arr[:, 0, 1] *= sy
            poly_int = arr.astype(np.int32)
            cv2.fillPoly(overlay, [poly_int], fill_bgr)
            # Area-weighted centroid via image moments. Degenerate
            # polygons (collinear points, etc.) get a bounding-box
            # center fallback so the label always lands somewhere
            # sensible.
            M = cv2.moments(poly_int)
            if M["m00"] != 0:
                centroid = (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))
            else:
                pts = poly_int.reshape(-1, 2)
                centroid = (
                    int((pts[:, 0].min() + pts[:, 0].max()) / 2),
                    int((pts[:, 1].min() + pts[:, 1].max()) / 2),
                )
            edges.append((edge_bgr, poly_int, label, centroid))
        self._cache[cam_id] = (sig, frame_w, frame_h, overlay, edges)
        return overlay, edges

    def invalidate(self, cam_id: str) -> None:
        self._cache.pop(cam_id, None)


def _edge_thickness(frame_h: int, frame_w: int) -> tuple[int, int]:
    # Scale stroke with frame's short side so the visual width is roughly constant
    # across resolutions. Floor at 1px so low-res streams stay thin instead of fat.
    short = min(frame_h, frame_w) if frame_h and frame_w else ZONE_EDGE_REF_DIM
    edge_t = max(1, round(ZONE_EDGE_BASE_PX * short / ZONE_EDGE_REF_DIM))
    return edge_t, edge_t + 2


def _bbox_thickness(frame_h: int, frame_w: int) -> int:
    """Vehicle bbox stroke. Thinner than the zone edge so the cars don't
    visually swallow each other in crowded frames — ~1px at 720p, 2px at
    1080p, 3px at 4K."""
    short = min(frame_h, frame_w) if frame_h and frame_w else 720
    return max(1, round(short / 720))


def _dwell_color(dwell_secs: float, threshold_secs: float) -> tuple[int, int, int]:
    """Green → yellow → red ramp based on dwell÷threshold.

    Returns green when there's no usable threshold or the track hasn't
    accumulated any dwell yet, so vehicles passing through without entering
    a timed zone stay neutral instead of misleadingly red.
    """
    if threshold_secs <= 0 or dwell_secs <= 0:
        return COLOR_NORMAL
    ratio = dwell_secs / threshold_secs
    if ratio >= DWELL_OVERSTAY_RATIO:
        return COLOR_OVERSTAY
    if ratio >= DWELL_WARN_RATIO:
        return COLOR_WARNING
    return COLOR_NORMAL


def _fmt_dwell(secs: float) -> str:
    s = max(0, int(secs))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m {s % 60}s"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


def _draw_annotations(
    frame: np.ndarray,
    tracked: list,
    zone_overlay: Optional[np.ndarray],
    zone_edges: list,
    hud_data: tuple,
    track_dwell: Optional[dict] = None,
    src_dim: Optional[tuple[int, int]] = None,
) -> np.ndarray:
    """Draw zone overlay + bboxes + HUD onto ``frame`` in-place.

    ``src_dim`` is the (w, h) of the frame the tracker ran on. If the SHM
    slot downscaled the frame (camera res > MAX_FRAME_W/H), we need to
    map bbox coords from source space into the actual frame space the
    renderer is drawing on. Without this, 2560x1440 cameras get bboxes
    drawn ~33% off-position because the tracker emitted coords in the
    original 2560-wide space.

    The zone-name label is rendered ONCE per polygon at its centroid
    (see _ZoneOverlayCache.get) — per-bbox tags would clutter the
    frame and repeat the same information for every vehicle in a zone.
    """
    inf_ms, fps = hud_data
    track_dwell = track_dwell or {}
    actual_h, actual_w = frame.shape[:2]
    if src_dim is not None and (src_dim[0] != actual_w or src_dim[1] != actual_h):
        sx = actual_w / float(src_dim[0])
        sy = actual_h / float(src_dim[1])
    else:
        sx = sy = 1.0

    if zone_overlay is not None:
        cv2.addWeighted(zone_overlay, ZONE_ALPHA, frame, 1.0 - ZONE_ALPHA, 0, frame)
    if zone_edges:
        edge_t, contrast_t = _edge_thickness(frame.shape[0], frame.shape[1])
        # Black contrast stroke first across every polygon, then the
        # zone-specific colored stroke on top — keeps edges legible
        # against any scene while preserving the per-zone color cue.
        contrast_polys = [poly for _, poly, _, _ in zone_edges]
        cv2.polylines(
            frame, contrast_polys, True,
            ZONE_EDGE_CONTRAST, contrast_t, cv2.LINE_AA,
        )
        # Zone-name label sits at each polygon's centroid in the zone's
        # palette colour, with a black halo for legibility against the
        # translucent fill. Bumped to 0.7 — visible at a glance on the
        # live preview without dominating the frame.
        zone_label_scale = 0.7
        zone_label_thick = 2
        for edge_bgr, poly, label, centroid in zone_edges:
            cv2.polylines(
                frame, [poly], True, edge_bgr, edge_t, cv2.LINE_AA,
            )
            if label and centroid:
                (tw, th), baseline = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX,
                    zone_label_scale, zone_label_thick,
                )
                anchor = (centroid[0] - tw // 2, centroid[1] + th // 2)
                # Black halo first (thicker stroke), then the coloured
                # text on top — same pattern as the alert image overlays
                # so zone names read clearly against any background.
                cv2.putText(
                    frame, label, anchor, cv2.FONT_HERSHEY_SIMPLEX,
                    zone_label_scale, ZONE_EDGE_CONTRAST,
                    zone_label_thick + 3, cv2.LINE_AA,
                )
                cv2.putText(
                    frame, label, anchor, cv2.FONT_HERSHEY_SIMPLEX,
                    zone_label_scale, edge_bgr,
                    zone_label_thick, cv2.LINE_AA,
                )

    bbox_t = _bbox_thickness(frame.shape[0], frame.shape[1])
    for item in tracked:
        track_id, x1, y1, x2, y2, conf = item[:6]
        cls_id = item[6] if len(item) > 6 else cfg.DEFAULT_VEHICLE_CLASS_ID
        x1 = int(x1 * sx)
        y1 = int(y1 * sy)
        x2 = int(x2 * sx)
        y2 = int(y2 * sy)
        cls_name = cfg.VEHICLE_CLASS_NAMES.get(cls_id, "vehicle")

        tid_int = int(track_id)
        # Accept either the new (dwell, threshold) tuple or the legacy bare
        # float so an analytics worker still on the old payload format
        # doesn't make the renderer crash mid-rollout.
        dwell_entry = track_dwell.get(tid_int)
        if isinstance(dwell_entry, tuple) and len(dwell_entry) >= 2:
            dwell_secs, dwell_thresh = float(dwell_entry[0]), float(dwell_entry[1])
        elif dwell_entry is not None:
            dwell_secs, dwell_thresh = float(dwell_entry), 0.0
        else:
            dwell_secs, dwell_thresh = 0.0, 0.0
        color = _dwell_color(dwell_secs, dwell_thresh)

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, bbox_t)
        label = f"{cls_name} #{tid_int}"
        if dwell_secs > 0:
            label = f"{label} | {_fmt_dwell(dwell_secs)}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        label_y1 = max(0, y1 - th - 8)
        label_y2 = max(th + 8, y1)
        cv2.rectangle(frame, (x1, label_y1), (x1 + tw + 8, label_y2), color, -1)
        cv2.putText(
            frame, label, (x1 + 4, max(th + 2, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2,
        )


    hud = f"Tracked: {len(tracked)}  FPS: {fps:.1f}  Inf: {inf_ms:.0f}ms"
    cv2.rectangle(frame, (0, 0), (len(hud) * 8 + 10, 22), (0, 0, 0), -1)
    cv2.putText(
        frame, hud, (5, 15),
        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
    )

    return frame


def _maybe_downscale(frame: np.ndarray, max_w: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= max_w:
        return frame
    new_w = max_w
    new_h = int(h * (max_w / w))
    return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)


# --------------------------------------------------------------------------
# Worker process
# --------------------------------------------------------------------------

def _renderer_proc(
    worker_idx: int,
    task_queue: "multiprocessing.Queue",
    frame_slots: dict,
    jpeg_slots: dict,
    cpu_set: set,
):
    """One UI worker. Pulls render signals; reads frames from SHM."""
    try:
        os.nice(10)
    except Exception:
        pass

    if cpu_set:
        from edge_agent.pipeline.cpu_topology import pin_current_thread
        pin_current_thread(set(cpu_set))

    proc_logger = logging.getLogger(f"ui-renderer-{worker_idx}")
    proc_logger.info(
        "UIRenderer worker %d started (pid=%d, cams=%d, cpu_set=%s)",
        worker_idx, os.getpid(), len(frame_slots), sorted(cpu_set) or None,
    )

    encoder = _JPEGEncoder(quality=cfg.LIVE_JPEG_QUALITY)
    overlay_cache = _ZoneOverlayCache()

    while True:
        try:
            task = task_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if task is None:
            break

        try:
            cam_id = task[0]
            tracked = task[1]
            zone_polys_canvas = task[2]
            frame_w, frame_h = task[3]
            hud_data = task[4]
            ts = task[5]
            track_dwell = task[6]

            frame_slot: Optional[FrameSlot] = frame_slots.get(cam_id)
            if frame_slot is None:
                continue
            frame, frame_ts = frame_slot.read()
            if frame is None:
                continue

            actual_h, actual_w = frame.shape[:2]

            zone_overlay, zone_edges = overlay_cache.get(
                cam_id, zone_polys_canvas, actual_w, actual_h,
            )

            annotated = _draw_annotations(
                frame, tracked, zone_overlay, zone_edges, hud_data, track_dwell,
                src_dim=(frame_w, frame_h),
            )
            annotated = _maybe_downscale(annotated, cfg.LIVE_PREVIEW_MAX_W)

            jpeg = encoder.encode(annotated)
            if jpeg is None:
                continue

            jpeg_slot: Optional[JPEGSlot] = jpeg_slots.get(cam_id)
            if jpeg_slot is not None:
                jpeg_slot.write(jpeg, ts)

        except Exception:
            proc_logger.exception("UIRenderer worker %d error", worker_idx)

    proc_logger.info("UIRenderer worker %d exiting", worker_idx)


# --------------------------------------------------------------------------
# Pool manager
# --------------------------------------------------------------------------

class UIRendererPool:
    """Manages one or more UIRenderer worker processes.

    A single shared task queue feeds all workers — any free worker grabs
    the next render signal. With one viewer at a time in practice, a
    single worker is plenty.
    
    Uses multiprocessing.Manager() dicts so that cameras added after
    worker spawn are visible to workers.
    """

    def __init__(
        self,
        num_workers: int = 1,
        queue_size: int = 32,
        cpu_set: Optional[set] = None,
    ):
        self._num_workers = max(1, int(num_workers))
        self._task_queue: multiprocessing.Queue = multiprocessing.Queue(
            maxsize=queue_size,
        )
        self._processes: list[multiprocessing.Process] = []
        
        # Use Manager() dicts for dynamic camera registration support
        manager = multiprocessing.Manager()
        self._frame_slots: dict[str, FrameSlot] = manager.dict()
        self._jpeg_slots: dict[str, JPEGSlot] = manager.dict()
        
        self._cpu_set = set(cpu_set) if cpu_set else set()

    @property
    def task_queue(self) -> multiprocessing.Queue:
        return self._task_queue

    def register_camera(
        self, cam_id: str, frame_slot: FrameSlot, jpeg_slot: JPEGSlot,
    ) -> None:
        self._frame_slots[cam_id] = frame_slot
        self._jpeg_slots[cam_id] = jpeg_slot

    def unregister_camera(self, cam_id: str) -> None:
        self._frame_slots.pop(cam_id, None)
        self._jpeg_slots.pop(cam_id, None)

    def start(self) -> None:
        if self._processes:
            return
        for idx in range(self._num_workers):
            p = multiprocessing.Process(
                target=_renderer_proc,
                args=(
                    idx,
                    self._task_queue,
                    self._frame_slots,  # Pass shared Manager() dict directly
                    self._jpeg_slots,   # Pass shared Manager() dict directly
                    self._cpu_set,
                ),
                daemon=True,
                name=f"UIRenderer-{idx}",
            )
            p.start()
            self._processes.append(p)
        logger.info("UIRendererPool started with %d worker(s)", self._num_workers)

    def stop(self) -> None:
        for _ in self._processes:
            try:
                self._task_queue.put_nowait(None)
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
