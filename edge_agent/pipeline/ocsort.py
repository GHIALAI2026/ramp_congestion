"""
OC-SORT: Observation-Centric SORT (Cao et al., 2023)

Drop-in replacement for ByteTrack. Same interface (update/reset).

Key advantages for queue tracking:
  1. ORU (Observation-Centric Re-Update) — corrects Kalman drift when
     re-finding lost tracks using interpolated virtual trajectory.
  2. OCM (Observation-Centric Momentum) — direction consistency from
     actual observations, not Kalman predictions.

A/B tested: OC-SORT wins 8-2 on dense overhead camera (cam-ov) with
32% fewer ID switches and 70% longer track lifetimes.

Reference: https://arxiv.org/abs/2203.14360
"""

from __future__ import annotations

from collections import deque

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist


# --------------------------------------------------------------------------
# Kalman Filter — same state as ByteTrack: [cx, cy, w, h, vcx, vcy, vw, vh]
# --------------------------------------------------------------------------

class KalmanFilter:
    ndim = 4

    def __init__(self):
        dt = 1.0
        self.F = np.eye(2 * self.ndim)
        for i in range(self.ndim):
            self.F[i, self.ndim + i] = dt
        self.H = np.eye(self.ndim, 2 * self.ndim)
        self._std_pos = 1.0 / 20.0
        self._std_vel = 1.0 / 160.0

    def initiate(self, bbox_cxcywh: np.ndarray):
        mean = np.concatenate([bbox_cxcywh, np.zeros(self.ndim)])
        w, h = bbox_cxcywh[2], bbox_cxcywh[3]
        std = np.array([
            2 * self._std_pos * w, 2 * self._std_pos * h,
            2 * self._std_pos * w, 2 * self._std_pos * h,
            10 * self._std_vel * w, 10 * self._std_vel * h,
            10 * self._std_vel * w, 10 * self._std_vel * h,
        ])
        cov = np.diag(std ** 2)
        return mean, cov

    def predict(self, mean, cov):
        w, h = mean[2], mean[3]
        std_pos = np.array([self._std_pos * w, self._std_pos * h,
                            self._std_pos * w, self._std_pos * h])
        std_vel = np.array([self._std_vel * w, self._std_vel * h,
                            self._std_vel * w, self._std_vel * h])
        Q = np.diag(np.concatenate([std_pos, std_vel]) ** 2)
        mean = self.F @ mean
        cov = self.F @ cov @ self.F.T + Q
        return mean, cov

    def update(self, mean, cov, measurement):
        w, h = mean[2], mean[3]
        std = np.array([self._std_pos * w, self._std_pos * h,
                        1e-1, self._std_pos * h])
        R = np.diag(std ** 2)
        S = self.H @ cov @ self.H.T + R
        # K = cov @ H.T @ inv(S). Solve S @ K.T = H @ cov.T instead of
        # materializing the inverse — faster for our 4×4 S and avoids
        # numerical blow-up when S is near-singular.
        K = np.linalg.solve(S, self.H @ cov.T).T
        innov = measurement - self.H @ mean
        mean = mean + K @ innov
        cov = (np.eye(2 * self.ndim) - K @ self.H) @ cov
        return mean, cov


_kf = KalmanFilter()

# --------------------------------------------------------------------------
# Track states
# --------------------------------------------------------------------------

_NEW = 0
_TRACKED = 1
_LOST = 2
_REMOVED = 3

_track_id_counter = 0


class OCSTrack:
    """Single object track with observation history for OC-SORT."""

    def __init__(self, bbox_xyxy: np.ndarray, conf: float, class_id: int = -1):
        global _track_id_counter
        _track_id_counter += 1
        self.track_id: int = _track_id_counter

        cx = (bbox_xyxy[0] + bbox_xyxy[2]) / 2
        cy = (bbox_xyxy[1] + bbox_xyxy[3]) / 2
        w = bbox_xyxy[2] - bbox_xyxy[0]
        h = bbox_xyxy[3] - bbox_xyxy[1]
        self._cxcywh = np.array([cx, cy, w, h], dtype=float)

        self.mean: np.ndarray | None = None
        self.cov: np.ndarray | None = None
        self.conf = conf
        self.class_id: int = int(class_id)
        self.state = _NEW
        self.frame_id = 0
        self.start_frame = 0
        self.time_since_update = 0
        self.hit_count = 0

        # OC-SORT: store observation history for ORU and OCM.
        # Bounded: velocity_direction uses last 2, ORU reads last only.
        # Unbounded lists here leaked ~540 KB per long-lived track.
        self._observations: deque[np.ndarray] = deque(maxlen=5)
        self._observation_frames: deque[int] = deque(maxlen=5)
        self._last_observation: np.ndarray | None = None

    @property
    def bbox_xyxy(self) -> np.ndarray:
        if self.mean is not None:
            cx, cy, w, h = self.mean[:4]
        else:
            cx, cy, w, h = self._cxcywh
        return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])

    @property
    def bbox_center(self) -> tuple[float, float]:
        if self.mean is not None:
            return float(self.mean[0]), float(self.mean[1])
        return float(self._cxcywh[0]), float(self._cxcywh[1])

    def activate(self, frame_id: int) -> None:
        self.mean, self.cov = _kf.initiate(self._cxcywh)
        self.state = _TRACKED
        self.frame_id = frame_id
        self.start_frame = frame_id
        self.hit_count = 1
        self._observations.append(self._cxcywh.copy())
        self._observation_frames.append(frame_id)
        self._last_observation = self._cxcywh.copy()

    def predict(self) -> None:
        if self.mean is not None:
            self.mean, self.cov = _kf.predict(self.mean, self.cov)
            self.time_since_update += 1

    def update(self, bbox_xyxy: np.ndarray, conf: float, frame_id: int,
               class_id: int = -1) -> None:
        cx = (bbox_xyxy[0] + bbox_xyxy[2]) / 2
        cy = (bbox_xyxy[1] + bbox_xyxy[3]) / 2
        w = bbox_xyxy[2] - bbox_xyxy[0]
        h = bbox_xyxy[3] - bbox_xyxy[1]
        meas = np.array([cx, cy, w, h], dtype=float)

        # OC-SORT ORU: if track was lost, re-update Kalman using virtual trajectory
        if self.time_since_update > 1 and self._last_observation is not None:
            self._observation_centric_reupdate(meas, frame_id)

        self.mean, self.cov = _kf.update(self.mean, self.cov, meas)
        self.conf = conf
        if class_id >= 0:
            self.class_id = int(class_id)
        self.state = _TRACKED
        self.frame_id = frame_id
        self.time_since_update = 0
        self.hit_count += 1

        self._observations.append(meas.copy())
        self._observation_frames.append(frame_id)
        self._last_observation = meas.copy()

    def _observation_centric_reupdate(self, new_obs: np.ndarray, new_frame: int) -> None:
        """
        ORU: Retroactively correct Kalman state using a virtual trajectory
        between the last real observation and the new one. This undoes the
        drift that accumulated during the lost period.
        """
        if self._last_observation is None or len(self._observation_frames) == 0:
            return

        last_obs = self._last_observation
        last_frame = self._observation_frames[-1]
        gap = new_frame - last_frame
        if gap <= 1:
            return

        # Re-initiate from last observation and step forward with virtual updates
        mean, cov = _kf.initiate(last_obs)

        # Interpolate observations across the gap
        for step in range(1, gap):
            alpha = step / gap
            virtual_obs = last_obs + alpha * (new_obs - last_obs)
            mean, cov = _kf.predict(mean, cov)
            mean, cov = _kf.update(mean, cov, virtual_obs)

        self.mean = mean
        self.cov = cov

    def mark_lost(self) -> None:
        self.state = _LOST

    def mark_removed(self) -> None:
        self.state = _REMOVED

    @property
    def velocity_direction(self) -> np.ndarray | None:
        """OCM: velocity from last two observations (not Kalman predictions)."""
        if len(self._observations) < 2:
            return None
        diff = self._observations[-1][:2] - self._observations[-2][:2]
        norm = np.linalg.norm(diff)
        if norm < 1e-6:
            return None
        return diff / norm


# --------------------------------------------------------------------------
# IoU helpers
# --------------------------------------------------------------------------

def _iou_batch(bboxes_a: np.ndarray, bboxes_b: np.ndarray) -> np.ndarray:
    if len(bboxes_a) == 0 or len(bboxes_b) == 0:
        return np.zeros((len(bboxes_a), len(bboxes_b)))
    area_a = (bboxes_a[:, 2] - bboxes_a[:, 0]) * (bboxes_a[:, 3] - bboxes_a[:, 1])
    area_b = (bboxes_b[:, 2] - bboxes_b[:, 0]) * (bboxes_b[:, 3] - bboxes_b[:, 1])
    inter_x1 = np.maximum(bboxes_a[:, None, 0], bboxes_b[None, :, 0])
    inter_y1 = np.maximum(bboxes_a[:, None, 1], bboxes_b[None, :, 1])
    inter_x2 = np.minimum(bboxes_a[:, None, 2], bboxes_b[None, :, 2])
    inter_y2 = np.minimum(bboxes_a[:, None, 3], bboxes_b[None, :, 3])
    inter_area = np.maximum(0, inter_x2 - inter_x1) * np.maximum(0, inter_y2 - inter_y1)
    union_area = area_a[:, None] + area_b[None, :] - inter_area
    return inter_area / np.maximum(union_area, 1e-6)


def _inflate_bboxes(bboxes: np.ndarray, factor: float) -> np.ndarray:
    cx = (bboxes[:, 0] + bboxes[:, 2]) / 2
    cy = (bboxes[:, 1] + bboxes[:, 3]) / 2
    w = (bboxes[:, 2] - bboxes[:, 0]) * factor
    h = (bboxes[:, 3] - bboxes[:, 1]) * factor
    return np.column_stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])


def _linear_assignment(cost_matrix: np.ndarray, thresh: float):
    if cost_matrix.size == 0:
        return [], list(range(cost_matrix.shape[0])), list(range(cost_matrix.shape[1]))
    row_ind, col_ind = linear_sum_assignment(cost_matrix)
    matches, unmatched_rows, unmatched_cols = [], [], []
    for r, c in zip(row_ind, col_ind):
        if cost_matrix[r, c] <= thresh:
            matches.append((r, c))
        else:
            unmatched_rows.append(r)
            unmatched_cols.append(c)
    for r in range(cost_matrix.shape[0]):
        if not any(r == m[0] for m in matches) and r not in unmatched_rows:
            unmatched_rows.append(r)
    for c in range(cost_matrix.shape[1]):
        if not any(c == m[1] for m in matches) and c not in unmatched_cols:
            unmatched_cols.append(c)
    return matches, unmatched_rows, unmatched_cols


# --------------------------------------------------------------------------
# OC-SORT Tracker
# --------------------------------------------------------------------------

TrackedDetection = tuple[int, float, float, float, float, float, int]


class OCSORTTracker:
    """
    OC-SORT tracker with ORU + OCM + velocity direction consistency.

    Same interface as BYTETracker for direct A/B comparison.
    """

    MIN_HITS = 3

    def __init__(
        self,
        high_thresh: float = 0.5,
        low_thresh: float = 0.1,
        match_thresh: float = 0.7,
        max_time_lost: int = 30,
        bbox_inflation: float = 2.0,
        ocm_weight: float = 0.3,
    ):
        self.high_thresh = high_thresh
        self.low_thresh = low_thresh
        self.match_thresh = match_thresh
        self.max_time_lost = max_time_lost
        self.bbox_inflation = bbox_inflation
        self.ocm_weight = ocm_weight  # weight for direction consistency cost

        self._tracked: list[OCSTrack] = []
        self._lost: list[OCSTrack] = []
        self._frame_id = 0

    def update(self, detections: list[tuple[float, float, float, float, float, int]]
               ) -> list[TrackedDetection]:
        self._frame_id += 1

        if not detections:
            self._age_tracks()
            return self._active_outputs()

        dets = np.array([[x1, y1, x2, y2, c, cls]
                         for x1, y1, x2, y2, c, cls in detections],
                        dtype=float)
        high_mask = dets[:, 4] >= self.high_thresh
        low_mask = (dets[:, 4] >= self.low_thresh) & ~high_mask

        high_dets = dets[high_mask]
        low_dets = dets[low_mask]

        for t in self._tracked + self._lost:
            t.predict()

        # Stage 1: match high-conf dets to all tracks (with OCM)
        all_tracks = self._tracked + self._lost
        matches1, unm_tracks1, unm_dets1 = self._match(high_dets, all_tracks)

        activated: list[OCSTrack] = []
        re_found: list[OCSTrack] = []
        lost_new: list[OCSTrack] = []

        for ti, di in matches1:
            track = all_tracks[ti]
            track.update(
                high_dets[di, :4], high_dets[di, 4], self._frame_id,
                class_id=int(high_dets[di, 5]),
            )
            if track in self._lost:
                re_found.append(track)
            else:
                activated.append(track)

        unm_tracked_tracks = [all_tracks[i] for i in unm_tracks1
                              if all_tracks[i] in self._tracked]

        # Stage 2: low-conf dets to remaining tracked
        if len(low_dets) > 0 and len(unm_tracked_tracks) > 0:
            matches2, unm_tracks2, _ = self._match(low_dets, unm_tracked_tracks)
            for ti, di in matches2:
                track = unm_tracked_tracks[ti]
                track.update(
                    low_dets[di, :4], low_dets[di, 4], self._frame_id,
                    class_id=int(low_dets[di, 5]),
                )
                activated.append(track)
            unm_tracked_tracks = [unm_tracked_tracks[i] for i in unm_tracks2]

        for track in unm_tracked_tracks:
            track.mark_lost()
            lost_new.append(track)

        removed: list[OCSTrack] = []
        for track in self._lost:
            if track.time_since_update > self.max_time_lost:
                track.mark_removed()
                removed.append(track)

        new_tracks: list[OCSTrack] = []
        for di in unm_dets1:
            if high_dets[di, 4] >= self.high_thresh:
                t = OCSTrack(
                    high_dets[di, :4], high_dets[di, 4],
                    class_id=int(high_dets[di, 5]),
                )
                t.activate(self._frame_id)
                new_tracks.append(t)

        self._tracked = activated + re_found + new_tracks
        re_found_ids = {t.track_id for t in re_found}
        removed_ids = {t.track_id for t in removed}
        self._lost = [t for t in self._lost
                      if t.track_id not in re_found_ids
                      and t.track_id not in removed_ids]
        self._lost += lost_new

        return self._active_outputs()

    def _match(self, dets: np.ndarray, tracks: list[OCSTrack]):
        # dets carry an extra class_id column now; we still match on bbox+conf.
        if len(dets) == 0 or len(tracks) == 0:
            return [], list(range(len(tracks))), list(range(len(dets)))

        dets_inflated = _inflate_bboxes(dets[:, :4], self.bbox_inflation)
        track_boxes = np.array([t.bbox_xyxy for t in tracks])
        track_boxes_inflated = _inflate_bboxes(track_boxes, self.bbox_inflation)

        iou = _iou_batch(track_boxes_inflated, dets_inflated)
        cost = 1.0 - iou

        # OCM: add velocity direction consistency penalty.
        # Vectorized over (T tracks × D detections) — same math as the
        # original double loop, computed in one NumPy expression.
        if self.ocm_weight > 0:
            det_centers = np.column_stack([
                (dets[:, 0] + dets[:, 2]) / 2,
                (dets[:, 1] + dets[:, 3]) / 2,
            ])  # (D, 2)
            T = len(tracks)
            vdirs = np.zeros((T, 2), dtype=float)
            tcs = np.zeros((T, 2), dtype=float)
            track_valid = np.zeros(T, dtype=bool)
            for ti, track in enumerate(tracks):
                v = track.velocity_direction
                if v is None:
                    continue
                vdirs[ti] = v
                tcs[ti] = track.bbox_center
                track_valid[ti] = True
            if track_valid.any():
                # Pairwise motion: det_center − track_center, shape (T, D, 2)
                motion = det_centers[None, :, :] - tcs[:, None, :]
                norms = np.linalg.norm(motion, axis=2)  # (T, D)
                motion_valid = norms >= 1e-6
                safe_norms = np.where(motion_valid, norms, 1.0)
                motion_dir = motion / safe_norms[:, :, None]
                # cos_sim[t, d] = vdirs[t] · motion_dir[t, d]
                cos_sim = np.sum(vdirs[:, None, :] * motion_dir, axis=2)
                direction_cost = self.ocm_weight * (1.0 - cos_sim) / 2.0
                apply_mask = motion_valid & track_valid[:, None]
                cost[apply_mask] += direction_cost[apply_mask]

        cost_thresh = 1.0 - self.match_thresh
        return _linear_assignment(cost, cost_thresh + self.ocm_weight)

    def _age_tracks(self) -> None:
        for t in self._tracked:
            t.predict()
            if t.time_since_update > 1:
                t.mark_lost()
                self._lost.append(t)
        self._tracked = [t for t in self._tracked if t.state == _TRACKED]
        for t in self._lost:
            if t.time_since_update > self.max_time_lost:
                t.mark_removed()
        self._lost = [t for t in self._lost if t.state != _REMOVED]

    def _active_outputs(self) -> list[TrackedDetection]:
        out = []
        for t in self._tracked:
            if t.state == _TRACKED and t.hit_count >= self.MIN_HITS:
                b = t.bbox_xyxy
                out.append((t.track_id, float(b[0]), float(b[1]),
                            float(b[2]), float(b[3]), float(t.conf),
                            int(t.class_id)))
        return out

    def reset(self) -> None:
        self._tracked.clear()
        self._lost.clear()
        self._frame_id = 0
