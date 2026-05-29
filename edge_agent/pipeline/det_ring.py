"""Lock-free shared-memory detection ring with per-camera multi-slot buffer.

Each camera has a small circular buffer of ``ring_depth`` sub-slots. The
producer (Voyager iter thread) writes the latest detection list to the
next sub-slot and advances the per-camera write head. The consumer
(analytics shard) tracks its own read index per camera and advances one
sub-slot per ``read_one`` call.

This restores temporal continuity at the tracker: every inferenced frame
gets a corresponding tracker.update() call, even when the shard briefly
stalls (GC pause, MQTT reconnect, OS scheduling jitter). The consumer
tolerates being up to ``ring_depth`` frames behind without loss.

Layout per camera slot:
    0    8 bytes  write_idx       uint64; producer's monotonic write head
    8    8 bytes  reserved        alignment padding
   16    ring_depth * sub_slot_bytes
                                  ring of K sub-slots

Layout per sub-slot (matches v1 single-slot layout exactly):
    0    8 bytes  seq             uint64 = absolute write position + 1
    8   28 bytes  body            ts_us(8), w(4), h(4), inf_ms(4), fps(4),
                                   num_dets(4)
   36    4 bytes  reserved        alignment padding
   40    max_dets * 24 bytes      detections (float32 × 6)

Single-producer per camera. We rely on x86 TSO so prior stores to the
sub-slot are observed before the seq store, and the sub-slot seq store
is observed before the write_idx store. On non-x86 platforms an
explicit fence would be required.
"""

from __future__ import annotations

import logging
import struct
from multiprocessing import shared_memory
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


SUB_SLOT_HEADER_BYTES = 40
PER_CAM_HEADER_BYTES = 16
DET_FIELDS = 6
DET_BYTES = DET_FIELDS * 4  # float32

_HEADER_BODY_FMT = "<qIIffI"        # ts_us, w, h, inf_ms, fps, num_dets
_HEADER_BODY_OFFSET = 8             # within sub-slot, after seq
_SEQ_FMT = "<Q"
_WRITE_IDX_FMT = "<Q"
_MAX_READ_RETRIES = 4


class DetectionRing:
    """Owner-side ring. Created once in the main process before fork.

    Children inherit the underlying mmap via the SharedMemory wrapper;
    pass the corresponding :class:`DetectionRingView` into worker procs.
    """

    def __init__(self, num_slots: int, max_dets: int, ring_depth: int = 16):
        self._num_slots = max(1, int(num_slots))
        self._max_dets = max(1, int(max_dets))
        self._ring_depth = max(2, int(ring_depth))
        self._sub_slot_bytes = SUB_SLOT_HEADER_BYTES + self._max_dets * DET_BYTES
        self._slot_bytes = (
            PER_CAM_HEADER_BYTES + self._ring_depth * self._sub_slot_bytes
        )
        total = self._slot_bytes * self._num_slots
        self._shm = shared_memory.SharedMemory(create=True, size=total)
        # Zero the buffer so write_idx and all seqs start at 0.
        self._shm.buf[:total] = b"\x00" * total
        logger.info(
            "DetectionRing created (slots=%d, ring_depth=%d, max_dets=%d, "
            "bytes=%d, name=%s)",
            self._num_slots, self._ring_depth, self._max_dets, total,
            self._shm.name,
        )

    @property
    def num_slots(self) -> int:
        return self._num_slots

    @property
    def max_dets(self) -> int:
        return self._max_dets

    @property
    def ring_depth(self) -> int:
        return self._ring_depth

    def view(self) -> "DetectionRingView":
        return DetectionRingView(
            self._shm, self._num_slots, self._max_dets, self._ring_depth,
        )

    def close(self) -> None:
        try:
            self._shm.close()
        except Exception:
            pass
        try:
            self._shm.unlink()
        except Exception:
            pass


class DetectionRingView:
    """Producer/consumer view. No ownership; safe to inherit via fork."""

    def __init__(self, shm, num_slots: int, max_dets: int, ring_depth: int):
        self._shm = shm
        self._num_slots = num_slots
        self._max_dets = max_dets
        self._ring_depth = ring_depth
        self._sub_slot_bytes = SUB_SLOT_HEADER_BYTES + max_dets * DET_BYTES
        self._slot_bytes = (
            PER_CAM_HEADER_BYTES + ring_depth * self._sub_slot_bytes
        )
        self._buf = shm.buf

    @property
    def max_dets(self) -> int:
        return self._max_dets

    @property
    def ring_depth(self) -> int:
        return self._ring_depth

    def _sub_offset(self, cam_offset: int, write_idx: int) -> int:
        return (
            cam_offset
            + PER_CAM_HEADER_BYTES
            + (write_idx % self._ring_depth) * self._sub_slot_bytes
        )

    # -- producer side --------------------------------------------------

    def write(
        self,
        slot: int,
        detections,
        frame_w: int,
        frame_h: int,
        ts: float,
        inf_ms: float,
        fps: float,
    ) -> bool:
        """Publish a fresh detection list for ``slot``.

        Single-writer per slot (the Voyager iter thread). Stores happen in
        this order:
          1. detections array
          2. body fields
          3. sub-slot seq (publishes the sub-slot)
          4. per-cam write_idx (publishes the new frame globally)
        """
        if slot < 0 or slot >= self._num_slots:
            return False
        cam_offset = slot * self._slot_bytes

        w = struct.unpack_from(_WRITE_IDX_FMT, self._buf, cam_offset)[0]
        sub_offset = self._sub_offset(cam_offset, w)

        # Accept either list[tuple] or an (n,6) ndarray. Plain truthiness
        # on an ndarray raises "ambiguous truth value"; use `is not None`
        # plus len(), which is well-defined for both.
        n = min(len(detections) if detections is not None else 0, self._max_dets)
        if n > 0:
            arr = np.asarray(detections[:n], dtype=np.float32)
            if arr.shape != (n, DET_FIELDS):
                arr = arr.reshape(n, DET_FIELDS)
            view = np.ndarray(
                (n, DET_FIELDS), dtype=np.float32,
                buffer=self._buf, offset=sub_offset + SUB_SLOT_HEADER_BYTES,
            )
            view[:] = arr

        ts_us = int(ts * 1_000_000)
        struct.pack_into(
            _HEADER_BODY_FMT, self._buf, sub_offset + _HEADER_BODY_OFFSET,
            ts_us, int(frame_w), int(frame_h),
            float(inf_ms), float(fps), n,
        )

        new_seq = (w + 1) & 0xFFFFFFFFFFFFFFFF
        # Publish the sub-slot first, then bump the per-cam write head.
        struct.pack_into(_SEQ_FMT, self._buf, sub_offset, new_seq)
        struct.pack_into(_WRITE_IDX_FMT, self._buf, cam_offset, new_seq)
        return True

    # -- consumer side --------------------------------------------------

    def read_one(
        self, slot: int, next_read_idx: int,
    ) -> Optional[tuple]:
        """Read the next-unread sub-slot for ``slot``.

        Advances one sub-slot per call. Returns
        ``(new_read_idx, dets, frame_w, frame_h, ts, inf_ms, fps)``
        where ``new_read_idx`` is what the consumer should pass on the
        next call. Returns ``None`` when the consumer is caught up
        (next_read_idx == write_idx).

        If the consumer is more than ``ring_depth`` frames behind, the
        oldest still-valid sub-slot is read instead and ``new_read_idx``
        jumps forward — older frames have been overwritten. Watch for
        this in throughput metrics; sustained occurrences indicate K is
        too small for the load.
        """
        if slot < 0 or slot >= self._num_slots:
            return None
        cam_offset = slot * self._slot_bytes

        w = struct.unpack_from(_WRITE_IDX_FMT, self._buf, cam_offset)[0]
        if w <= next_read_idx:
            return None  # caught up

        if w - next_read_idx > self._ring_depth:
            # Overflow: target sub-slot has been overwritten. Jump to the
            # oldest still-valid index (= w - ring_depth).
            next_read_idx = w - self._ring_depth

        sub_offset = self._sub_offset(cam_offset, next_read_idx)
        expected_seq = (next_read_idx + 1) & 0xFFFFFFFFFFFFFFFF

        for _ in range(_MAX_READ_RETRIES):
            seq_a = struct.unpack_from(_SEQ_FMT, self._buf, sub_offset)[0]
            if seq_a != expected_seq:
                # Producer either hasn't reached this position yet (rare
                # under x86 TSO since w > next_read_idx implies the
                # sub-slot has been published) or has wrapped past us.
                # Re-anchor and retry.
                w = struct.unpack_from(_WRITE_IDX_FMT, self._buf, cam_offset)[0]
                if w <= next_read_idx:
                    return None
                if w - next_read_idx > self._ring_depth:
                    next_read_idx = w - self._ring_depth
                    sub_offset = self._sub_offset(cam_offset, next_read_idx)
                    expected_seq = (next_read_idx + 1) & 0xFFFFFFFFFFFFFFFF
                continue

            ts_us, frame_w, frame_h, inf_ms, fps, num_dets = struct.unpack_from(
                _HEADER_BODY_FMT, self._buf, sub_offset + _HEADER_BODY_OFFSET,
            )
            n = min(num_dets, self._max_dets)
            if n > 0:
                view = np.ndarray(
                    (n, DET_FIELDS), dtype=np.float32,
                    buffer=self._buf, offset=sub_offset + SUB_SLOT_HEADER_BYTES,
                )
                dets = view.copy()
            else:
                dets = np.empty((0, DET_FIELDS), dtype=np.float32)

            seq_b = struct.unpack_from(_SEQ_FMT, self._buf, sub_offset)[0]
            if seq_b == seq_a:
                return (
                    next_read_idx + 1,
                    dets,
                    int(frame_w),
                    int(frame_h),
                    ts_us / 1_000_000.0,
                    float(inf_ms),
                    float(fps),
                )
            # Producer raced us mid-read; retry.
        return None
