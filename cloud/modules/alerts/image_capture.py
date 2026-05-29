"""Capture evidence snapshots for alerts.

For overstay alerts, the offending vehicle is cropped from the latest frame
using the bbox supplied by the edge. For overcrowding alerts, the full frame
is saved. Files land under ``cloud/static/alert_images/`` so they're served by
the existing FastAPI static mount at ``/static/alert_images/{alert_id}.jpg``.

Each capture fetches a few frames spaced ~250ms apart and keeps the sharpest
(Laplacian variance) — motion blur and momentary occlusion are common at the
exact alert instant, and three cheap probes are far better than one. The
saved image is watermarked with cam_id | zone_id | timestamp | dwell | track
so the evidence stays linkable to its alert row when exported.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

import cv2
import httpx
import numpy as np

from cloud.config import settings
from cloud.models.db import get_db

logger = logging.getLogger(__name__)


async def _resolve_zone_label(zone_id: Optional[str]) -> Optional[str]:
    """Return the operator-facing display name for a zone_id.

    The watermark used to print the raw zone_id ("arrival_zone_11"),
    which doesn't match what the operator sees on the dashboard
    ("Zone 11"). Looks up the zone, prefers the group's name when
    the zone is in a group, falls back to the zone's own name, and
    finally the raw id if nothing useful is stored.
    """
    if not zone_id:
        return None
    try:
        db = await get_db()
        row = await db.fetchrow(
            """SELECT z.name AS zone_name, g.name AS group_name
               FROM zones z
               LEFT JOIN zone_groups g ON g.group_id = z.zone_group_id
               WHERE z.zone_id = $1""",
            zone_id,
        )
    except Exception:
        # Image capture is best-effort — if the lookup fails for any
        # reason, fall back to the raw id so the watermark is still
        # produced (just with the old text).
        return zone_id
    if row is None:
        return zone_id
    return row["group_name"] or row["zone_name"] or zone_id


_ALERT_IMG_DIR = Path(__file__).resolve().parents[2] / "static" / "alert_images"
_ALERT_IMG_DIR.mkdir(parents=True, exist_ok=True)

# Base margin (fraction of bbox dimension) added when cropping a vehicle so
# the crop has visual context around the car.
_BBOX_MARGIN = 0.20

# Distant vehicles get extra margin so the plate/context stays readable —
# the effective margin is grown so the crop has at least this many context
# pixels per side relative to the bbox width. Sized so a 60-px bbox becomes
# a ~380-px crop (vs. ~84 px at margin 0.20) and the operator can identify
# which spot in the ramp the car is sitting in.
_MIN_CONTEXT_PX = 160

# Minimum crop size (pixels) — small bboxes get padded to this before saving.
# 320 keeps even the tiniest bbox usable as evidence (plate + immediate
# surroundings); below ~300 px the saved image is just a thumbnail.
_MIN_CROP_PX = 320

# Hard cap on crop dimensions as a fraction of the source frame, so a
# single-vehicle overstay crop never grows to look like a full overcrowding
# shot. 70% leaves clear "this is a crop" visual cues and avoids the
# degenerate case of bbox+margin spilling past the frame on both sides.
_MAX_CROP_FRAC = 0.70

# BGR red for the bbox drawn around the offending vehicle inside the crop.
# Matches the dwell-overstay color used by the live UI renderer so the
# operator's eye associates the two views.
_OFFENDER_COLOR = (40, 40, 240)

# Best-of-N snapshot picking. Three quick fetches catch the case where the
# first frame is blurred / occluded; spacing is large enough that the next
# frame is genuinely different but small enough that total capture stays
# under ~1s.
_NUM_SNAPSHOT_ATTEMPTS = 3
_SNAPSHOT_SPACING_S = 0.25

# Saved JPEG quality — slightly higher than default (85) because we now
# always decode + re-encode (for watermark / crop) so the round-trip needs
# headroom to not visibly degrade the evidence.
_JPEG_QUALITY = 92


async def capture_alert_image(
    alert_id: int,
    camera_id: str,
    alert_type: str,
    bbox: Optional[Sequence[float]] = None,
    *,
    zone_id: Optional[str] = None,
    ts: Optional[float] = None,
    dwell_time_s: Optional[float] = None,
    track_id: Optional[int] = None,
    frame_w: Optional[int] = None,
    frame_h: Optional[int] = None,
    zone_poly: Optional[Sequence[Sequence[float]]] = None,
) -> Optional[str]:
    """Fetch frames from the edge, pick the sharpest, crop+watermark, save.

    Returns the public URL (relative path) on success, or None on any
    failure. Failures are logged but never raised — image capture is
    best-effort and should never block alert insertion.
    """
    try:
        # Source endpoint per alert type — NO cross-fallback:
        #   * overstay     → /snapshot only. We want the cropped vehicle,
        #                    which requires a raw frame. Falling back to
        #                    /annotated would mean cropping a bbox-overlaid
        #                    frame and the result is a tiny chunk of an
        #                    annotated image with overlapping labels.
        #   * overcrowding → /annotated only. Operator wants to see all
        #                    tracked vehicles inside the polygon as context.
        path = "snapshot" if alert_type == "overstay" else "annotated"
        url = f"{settings.edge_http_base_url}/{path}/{camera_id}"

        frames = await _fetch_snapshots(url)
        if not frames:
            logger.warning(
                "no snapshot from %s for cam %s (alert %s) — skipping image",
                url, camera_id, alert_id,
            )
            return None

        # The edge's shm preview slot caps frames at MAX_FRAME_W/H so a 1440p
        # camera arrives here as a ~1080p snapshot, while `bbox` is in the
        # 1440p source-frame coordinate space. Rescale to snapshot pixels
        # before any crop/draw, otherwise the bbox lands on empty road.
        scaled_bbox: Optional[Sequence[float]] = bbox
        scaled_poly: Optional[list[tuple[float, float]]] = None
        if (
            bbox is not None and len(bbox) == 4
            and frame_w and frame_h
        ):
            img_h, img_w = frames[0].shape[:2]
            if frame_w != img_w or frame_h != img_h:
                sx = img_w / float(frame_w)
                sy = img_h / float(frame_h)
                scaled_bbox = [
                    bbox[0] * sx, bbox[1] * sy,
                    bbox[2] * sx, bbox[3] * sy,
                ]
        # Same scale applies to the zone polygon — the edge sent it in
        # source-frame coords so cloud just maps frame → snapshot once.
        if (
            zone_poly and len(zone_poly) >= 3
            and frame_w and frame_h
        ):
            img_h2, img_w2 = frames[0].shape[:2]
            psx = img_w2 / float(frame_w)
            psy = img_h2 / float(frame_h)
            scaled_poly = [(float(p[0]) * psx, float(p[1]) * psy) for p in zone_poly]

        score_bbox = scaled_bbox if alert_type == "overstay" else None
        best = max(frames, key=lambda f: _sharpness(f, score_bbox))

        if alert_type == "overstay" and scaled_bbox is not None and len(scaled_bbox) == 4:
            # Pre-format the dwell so _crop_bbox_arr can paint it as a
            # red chip above the offender bbox. Strip caption no longer
            # carries the dwell text — putting it on the vehicle ties
            # "how long" directly to "which car" for the operator.
            dwell_label = (
                _fmt_dwell(dwell_time_s) if dwell_time_s is not None else None
            )
            cropped, crop_origin = _crop_bbox_arr(
                best, scaled_bbox, dwell_label=dwell_label,
            )
            if cropped is None:
                return None
            out_img = cropped
            # Deliberately NOT drawing the zone polygon here: the overstay
            # crop is tight around the bbox, so the polygon edge mostly
            # cuts through empty crop margin and looks like clutter. The
            # red offender bbox already pinpoints the vehicle; operators
            # don't need the polygon outline on top of it.
        else:
            # Overcrowding alerts save the full frame — drawing the zone
            # outline there is genuinely useful (shows the operator
            # exactly which zone is congested without making them
            # cross-reference a map).
            out_img = best
            if scaled_poly:
                _draw_zone_outline(out_img, scaled_poly, crop_origin=(0, 0))

        zone_label = await _resolve_zone_label(zone_id)
        out_img = _watermark(
            out_img,
            camera_id=camera_id,
            zone_id=zone_id,
            zone_label=zone_label,
            ts=ts,
            dwell_time_s=dwell_time_s,
            track_id=track_id,
        )

        ok, buf = cv2.imencode(".jpg", out_img, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
        if not ok:
            return None
        out_path = _ALERT_IMG_DIR / f"{alert_id}.jpg"
        out_path.write_bytes(bytes(buf))
        return f"/static/alert_images/{alert_id}.jpg"
    except Exception:
        logger.exception("Failed to capture alert image for %s alert %s",
                         alert_type, alert_id)
        return None


async def _fetch_snapshots(url: str) -> list[np.ndarray]:
    """Fetch up to _NUM_SNAPSHOT_ATTEMPTS frames from `url`, decoded."""
    frames: list[np.ndarray] = []
    async with httpx.AsyncClient(timeout=2.5) as client:
        for attempt in range(_NUM_SNAPSHOT_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(_SNAPSHOT_SPACING_S)
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("alert image fetch attempt %d %s failed: %s",
                               attempt, url, exc)
                continue
            if resp.status_code != 200 or not resp.content:
                logger.debug("alert image fetch attempt %d %s returned %s",
                             attempt, url, resp.status_code)
                continue
            arr = np.frombuffer(resp.content, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                frames.append(img)
    return frames


def _sharpness(img: np.ndarray, bbox: Optional[Sequence[float]] = None) -> float:
    """Laplacian variance of `img` (or its bbox region). Higher = sharper."""
    region = img
    if bbox is not None and len(bbox) == 4:
        h, w = img.shape[:2]
        try:
            x1, y1, x2, y2 = (int(round(float(v))) for v in bbox)
        except (TypeError, ValueError):
            x1 = y1 = x2 = y2 = 0
        x1 = max(0, x1); y1 = max(0, y1)
        x2 = min(w, x2); y2 = min(h, y2)
        if x2 > x1 and y2 > y1:
            region = img[y1:y2, x1:x2]
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _crop_bbox_arr(
    img: np.ndarray, bbox: Sequence[float],
    dwell_label: Optional[str] = None,
) -> tuple[Optional[np.ndarray], tuple[int, int]]:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    # Scale margin up for small bboxes so distant cars keep readable
    # plate/context; horizontal context is what matters for vehicles, so
    # the margin is keyed off bbox width and applied to both axes.
    margin = max(_BBOX_MARGIN, _MIN_CONTEXT_PX / bw)
    mx = bw * margin
    my = bh * margin
    cx, cy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
    # Floor by _MIN_CROP_PX so small bboxes get usable evidence; cap by
    # _MAX_CROP_FRAC * frame so even a huge bbox+margin doesn't degrade
    # into a full-frame shot.
    max_half_w = w * _MAX_CROP_FRAC * 0.5
    max_half_h = h * _MAX_CROP_FRAC * 0.5
    half_w = min(max_half_w, max(bw * 0.5 + mx, _MIN_CROP_PX * 0.5))
    half_h = min(max_half_h, max(bh * 0.5 + my, _MIN_CROP_PX * 0.5))
    cx1 = int(max(0, round(cx - half_w)))
    cy1 = int(max(0, round(cy - half_h)))
    cx2 = int(min(w, round(cx + half_w)))
    cy2 = int(min(h, round(cy + half_h)))
    if cx2 <= cx1 or cy2 <= cy1:
        return None, (0, 0)
    crop = img[cy1:cy2, cx1:cx2].copy()

    # Mark the offending vehicle inside the crop so an operator can tell
    # which car the alert refers to when the crop happens to contain
    # multiple vehicles (overstay scenes typically have nearby parked
    # cars). Bright red, scaled with crop short-side so it stays visible
    # at any size. Drawn on the crop AFTER copy so the source frame is
    # left clean for any other consumer.
    lx1 = int(round(x1 - cx1))
    ly1 = int(round(y1 - cy1))
    lx2 = int(round(x2 - cx1))
    ly2 = int(round(y2 - cy1))
    ch, cw = crop.shape[:2]
    lx1 = max(0, min(cw - 1, lx1))
    ly1 = max(0, min(ch - 1, ly1))
    lx2 = max(0, min(cw - 1, lx2))
    ly2 = max(0, min(ch - 1, ly2))
    if lx2 > lx1 and ly2 > ly1:
        stroke = max(2, round(min(cw, ch) / 200))
        cv2.rectangle(crop, (lx1, ly1), (lx2, ly2), _OFFENDER_COLOR, stroke, cv2.LINE_AA)
        # Dwell chip sitting just above the bbox's top edge — red fill
        # matches the bbox stroke so the eye reads "vehicle + how long"
        # as one unit. Falls back to *inside* the top of the bbox if
        # there isn't room above (bbox flush with the crop top edge).
        if dwell_label:
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = max(0.4, min(0.7, min(cw, ch) / 800))
            font_thick = max(1, round(min(cw, ch) / 500))
            (tw, th), _ = cv2.getTextSize(dwell_label, font, font_scale, font_thick)
            pad_x, pad_y = 6, 3
            chip_w = tw + 2 * pad_x
            chip_h = th + 2 * pad_y
            chip_x1 = max(0, min(cw - chip_w, lx1))
            if ly1 - chip_h - 2 >= 0:
                chip_y1 = ly1 - chip_h - 2
            else:
                chip_y1 = ly1 + 2  # bbox at frame top — tuck chip inside
            chip_x2 = chip_x1 + chip_w
            chip_y2 = chip_y1 + chip_h
            cv2.rectangle(
                crop, (chip_x1, chip_y1), (chip_x2, chip_y2),
                _OFFENDER_COLOR, -1,
            )
            cv2.putText(
                crop, dwell_label,
                (chip_x1 + pad_x, chip_y2 - pad_y - 1),
                font, font_scale, (255, 255, 255), font_thick, cv2.LINE_AA,
            )
    return crop, (cx1, cy1)


# BGR for the zone-outline stroke on alert evidence images. A muted cyan so
# the outline is clearly a zone (not a vehicle box), with a thinner stroke
# than the offender bbox so the eye still goes to the vehicle first.
_ZONE_OUTLINE_COLOR = (200, 180, 60)


def _draw_zone_outline(
    img: np.ndarray,
    poly_snap: Sequence[tuple[float, float]],
    crop_origin: tuple[int, int],
) -> None:
    """Draw the alert's zone polygon on the (possibly cropped) image.

    ``poly_snap`` is already in *snapshot* pixel coords (the edge sent it
    in frame coords; the caller rescaled by snap.w/frame_w). For a crop,
    ``crop_origin`` is (cx1, cy1) so we can translate snapshot coords
    into crop-local coords. For an uncropped full-frame image, pass
    (0, 0). Polygon vertices that land outside the image rectangle are
    clipped naturally by OpenCV.
    """
    if len(poly_snap) < 3:
        return
    ox, oy = crop_origin
    pts = np.array(
        [(x - ox, y - oy) for x, y in poly_snap],
        dtype=np.int32,
    ).reshape(-1, 1, 2)
    h, w = img.shape[:2]
    stroke = max(1, round(min(h, w) / 320))
    # Black contrast halo, then the colored stroke on top — matches the
    # pattern the edge UI renderer uses so the look is consistent
    # between live previews and saved evidence.
    cv2.polylines(img, [pts], True, (0, 0, 0), stroke + 2, cv2.LINE_AA)
    cv2.polylines(img, [pts], True, _ZONE_OUTLINE_COLOR, stroke, cv2.LINE_AA)


def _fmt_dwell(secs: float) -> str:
    s = max(0, int(secs))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m {s % 60}s"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


def _watermark(
    img: np.ndarray,
    *,
    camera_id: str,
    zone_id: Optional[str],
    ts: Optional[float],
    dwell_time_s: Optional[float],
    track_id: Optional[int],
    zone_label: Optional[str] = None,
) -> np.ndarray:
    """Append a metadata strip below the image (does not overlay pixels)."""
    # Strip carries scope identifiers (cam, zone, alert ts, arrival ts,
    # track id). Dwell deliberately omitted — it's drawn as a red chip
    # above the offender bbox so the operator's eye gets the duration
    # at the same time it lands on the vehicle, instead of having to
    # ping-pong between the strip text and the bbox.
    # Prefer the friendly zone label (e.g. "Zone 11") over the raw
    # zone_id ("arrival_zone_11") — the latter is meaningless to operators.
    parts: list[str] = [camera_id]
    if zone_label or zone_id:
        parts.append(zone_label or zone_id)
    if ts is not None:
        # Local server time (no tz suffix) so this matches the wall-clock the
        # edge stamps on the top-right of every preview frame and the rest of
        # the dashboard. Previously formatted as UTC ("…Z") which made the
        # caption read 5h 30m off on IST deployments and looked broken.
        parts.append(
            datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        )
    if ts is not None and dwell_time_s is not None:
        # arrival = alert fire time minus accrued dwell. Same-day arrivals
        # show as HH:MM:SS; older arrivals add the date so the caption is
        # never ambiguous when reviewing evidence days later.
        arrival_dt = datetime.fromtimestamp(ts - float(dwell_time_s))
        alert_dt = datetime.fromtimestamp(ts)
        arrival_fmt = (
            arrival_dt.strftime("%H:%M:%S")
            if arrival_dt.date() == alert_dt.date()
            else arrival_dt.strftime("%Y-%m-%d %H:%M:%S")
        )
        parts.append(f"arrived {arrival_fmt}")
    if track_id is not None:
        parts.append(f"#{track_id}")
    # ASCII pipe instead of U+00B7 middle dot: the Hershey-Simplex font
    # cv2.putText uses has no glyph for non-ASCII chars, so middle dots
    # render as "??" in the saved JPEG. "|" is in the font.
    text = "  |  ".join(parts)

    h, w = img.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.5
    thickness = 1
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    # Auto-shrink for narrow crops. The previous 0.32 floor still let text
    # clip off the right edge on ~400-px crops, dropping dwell and #track —
    # operators noticed. Lower the floor enough to fit a typical caption in
    # a 400-px-wide crop without losing legibility.
    if tw > w - 12:
        scale = max(0.28, scale * (w - 12) / tw)
        (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)

    # If the caption still overflows at the scale floor (very narrow crops on
    # 4K cameras), widen the canvas with symmetric black side-bars so the
    # strip always shows the full text. The image itself is left untouched.
    strip_h = th + baseline + 12
    canvas_w = max(w, tw + 12)
    canvas = np.zeros((h + strip_h, canvas_w, 3), dtype=np.uint8)
    img_x = (canvas_w - w) // 2
    canvas[:h, img_x:img_x + w] = img
    text_x = max(6, (canvas_w - tw) // 2)
    text_y = h + th + 6
    cv2.putText(canvas, text, (text_x, text_y), font, scale,
                (255, 255, 255), thickness, cv2.LINE_AA)
    return canvas
