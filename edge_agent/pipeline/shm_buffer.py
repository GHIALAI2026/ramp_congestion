"""SharedMemory-backed frame and JPEG buffers for cross-process IPC.

Avoids pickling 1080p frames through multiprocessing.Queue. The producer
(Voyager iter thread) writes pixels into a per-camera SHM segment;
consumers (UI worker, HTTP server) attach by name and read with a single
memcpy.

Layout:
  FrameSlot  — one double-buffered SHM block per camera (raw BGR pixels).
  JPEGSlot   — one SHM block per camera (latest annotated JPEG bytes).

Synchronization is a seqlock stored in a small dedicated SHM metadata
block (no ``multiprocessing.Array``). The writer increments the version
to an odd value before mutating, then to the next even value after. The
reader retries while the version is odd or changes mid-read. This makes
the whole slot picklable — instances serialize to just the SHM names —
so the slots can flow through ``multiprocessing.Manager().dict()`` for
dynamic camera registration. Workers attach to the existing segments
on the consumer side instead of creating new ones.

Single-producer per slot. We rely on x86 TSO for store ordering between
the pixel/JPEG bytes and the meta version bump; on non-x86 platforms an
explicit fence would be required.
"""

from __future__ import annotations

import logging
import struct
from multiprocessing import shared_memory
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Frame slot — raw BGR pixels, double-buffered
# --------------------------------------------------------------------------

# Metadata layout (all int64, little-endian) — 6 fields × 8 B = 48 B
#   0  active     buffer index (0 or 1)
#   1  ts_high    high 32 bits of timestamp (microseconds)
#   2  ts_low     low 32 bits of timestamp (microseconds)
#   3  width      current frame width
#   4  height     current frame height
#   5  version    seqlock counter: odd = write in progress, even = stable
_FRAME_META_FIELDS = 6
_FRAME_META_BYTES = _FRAME_META_FIELDS * 8
_SEQLOCK_MAX_RETRIES = 8


class FrameSlot:
    """Per-camera double-buffered frame in shared memory.

    The producer writes to whichever buffer is currently inactive, then
    flips the ``active`` index. Readers always pick up the most recent
    fully-written buffer via the seqlock dance.

    Sized for the worst-case resolution at construction time. Frames
    larger than the slot are silently dropped (logged once).
    """

    def __init__(
        self,
        cam_id: str,
        max_h: int,
        max_w: int,
        channels: int = 3,
        _attach: Optional[tuple[str, str, str]] = None,
    ):
        self.cam_id = cam_id
        self.max_h = max_h
        self.max_w = max_w
        self._channels = channels
        self._slot_bytes = max_h * max_w * channels

        if _attach is None:
            # Owner-side: create fresh SHM segments.
            self._shm_a = shared_memory.SharedMemory(create=True, size=self._slot_bytes)
            self._shm_b = shared_memory.SharedMemory(create=True, size=self._slot_bytes)
            self._meta = shared_memory.SharedMemory(create=True, size=_FRAME_META_BYTES)
            self._meta.buf[:_FRAME_META_BYTES] = b"\x00" * _FRAME_META_BYTES
            self._owner = True
        else:
            # Consumer-side: attach to existing segments by name.
            a_name, b_name, meta_name = _attach
            self._shm_a = shared_memory.SharedMemory(name=a_name)
            self._shm_b = shared_memory.SharedMemory(name=b_name)
            self._meta = shared_memory.SharedMemory(name=meta_name)
            self._owner = False

        self._oversize_logged = False
        # Single-writer state — which buffer the next write() will fill.
        # Updated only by the producer thread; readers ignore it.
        self._next_inactive = 1

    # -- pickling ------------------------------------------------------

    def __getstate__(self) -> dict:
        # Slots pickle as the SHM names + sizing — the consumer side
        # attaches to the existing segments instead of recreating them.
        return {
            "cam_id": self.cam_id,
            "max_h": self.max_h,
            "max_w": self.max_w,
            "channels": self._channels,
            "shm_a": self._shm_a.name,
            "shm_b": self._shm_b.name,
            "meta": self._meta.name,
        }

    def __setstate__(self, state: dict) -> None:
        self.__init__(
            state["cam_id"],
            state["max_h"],
            state["max_w"],
            channels=state["channels"],
            _attach=(state["shm_a"], state["shm_b"], state["meta"]),
        )

    # -- producer side -------------------------------------------------

    def write(self, frame: np.ndarray, ts: float) -> bool:
        """Copy ``frame`` into the inactive buffer and publish via seqlock.

        Single-writer: the iter thread is the only producer. The frame
        memcpy targets the inactive buffer, which no reader can be
        looking at, so it runs without coordination. The version bumps
        on either side of the meta update provide the publish barrier.
        """
        if frame is None or frame.ndim != 3:
            return False
        h, w, c = frame.shape
        if c != self._channels:
            if not self._oversize_logged:
                logger.warning(
                    "[shm] %s channel mismatch frame=%d slot=%d — dropping",
                    self.cam_id, c, self._channels,
                )
                self._oversize_logged = True
            return False
        if h > self.max_h or w > self.max_w:
            # Auto-downscale instead of dropping. Without this, any camera
            # publishing above MAX_FRAME_W/H (e.g. 2560x1440) silently never
            # makes it into the UI renderer's input slot, the annotated
            # JPEG slot stays empty, and /annotated falls back to encoding
            # the raw frame — operator sees a video feed but no overlays.
            # Preserve aspect ratio; AREA interpolation gives the cleanest
            # result for the >2x shrinks we typically hit here.
            scale = min(self.max_h / h, self.max_w / w)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            if not self._oversize_logged:
                logger.warning(
                    "[shm] %s frame %dx%d > slot %dx%d; downscaling to %dx%d "
                    "for preview (inference is unaffected)",
                    self.cam_id, w, h, self.max_w, self.max_h, new_w, new_h,
                )
                self._oversize_logged = True
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
            h, w = new_h, new_w

        inactive = self._next_inactive
        shm = self._shm_a if inactive == 0 else self._shm_b
        arr = np.ndarray((h, w, c), dtype=np.uint8, buffer=shm.buf)
        arr[:] = frame  # ~6 MB memcpy

        # Current version is even (stable). Bump to odd, write fields,
        # bump to next even. Readers will retry over the odd window.
        ver = struct.unpack_from("<q", self._meta.buf, 40)[0]
        struct.pack_into("<q", self._meta.buf, 40, ver + 1)         # odd
        ts_int = int(ts * 1_000_000)
        struct.pack_into("<q", self._meta.buf, 0, inactive)         # active
        struct.pack_into("<q", self._meta.buf, 8, (ts_int >> 32) & 0xFFFFFFFF)
        struct.pack_into("<q", self._meta.buf, 16, ts_int & 0xFFFFFFFF)
        struct.pack_into("<q", self._meta.buf, 24, w)
        struct.pack_into("<q", self._meta.buf, 32, h)
        struct.pack_into("<q", self._meta.buf, 40, ver + 2)         # even

        self._next_inactive = 1 - inactive
        return True

    # -- consumer side -------------------------------------------------

    def read(self) -> tuple[Optional[np.ndarray], float]:
        """Return a copy of the latest frame and its timestamp.

        Returns (None, 0.0) when no frame has been written yet or the
        seqlock cannot stabilise within the retry budget (extreme
        contention; never observed in practice with a single writer).
        """
        for _ in range(_SEQLOCK_MAX_RETRIES):
            v1 = struct.unpack_from("<q", self._meta.buf, 40)[0]
            if v1 & 1:
                continue  # writer mid-flight; spin
            active = struct.unpack_from("<q", self._meta.buf, 0)[0]
            ts_high = struct.unpack_from("<q", self._meta.buf, 8)[0]
            ts_low = struct.unpack_from("<q", self._meta.buf, 16)[0]
            w = struct.unpack_from("<q", self._meta.buf, 24)[0]
            h = struct.unpack_from("<q", self._meta.buf, 32)[0]
            v2 = struct.unpack_from("<q", self._meta.buf, 40)[0]
            if v1 != v2:
                continue  # writer raced us; retry
            if w == 0 or h == 0:
                return None, 0.0
            shm = self._shm_a if active == 0 else self._shm_b
            arr = np.ndarray((h, w, self._channels), dtype=np.uint8, buffer=shm.buf)
            ts_int = (ts_high << 32) | (ts_low & 0xFFFFFFFF)
            return arr.copy(), ts_int / 1_000_000.0
        return None, 0.0

    def version(self) -> int:
        return struct.unpack_from("<q", self._meta.buf, 40)[0]

    def close(self) -> None:
        for shm in (self._shm_a, self._shm_b, self._meta):
            try:
                shm.close()
            except Exception:
                pass
            if self._owner:
                try:
                    shm.unlink()
                except Exception:
                    pass


# --------------------------------------------------------------------------
# JPEG slot — bytes payload of the most recent annotated frame
# --------------------------------------------------------------------------

# Metadata layout — 3 int64 fields × 8 B = 24 B
#   0  length     bytes written (0 = empty)
#   1  ts_int     microsecond timestamp
#   2  version    seqlock counter
_JPEG_META_FIELDS = 3
_JPEG_META_BYTES = _JPEG_META_FIELDS * 8


class JPEGSlot:
    """Latest annotated JPEG for one camera, in shared memory."""

    def __init__(
        self,
        cam_id: str,
        max_size: int = 2 * 1024 * 1024,
        _attach: Optional[tuple[str, str]] = None,
    ):
        self.cam_id = cam_id
        self.max_size = max_size

        if _attach is None:
            self._shm = shared_memory.SharedMemory(create=True, size=max_size)
            self._meta = shared_memory.SharedMemory(create=True, size=_JPEG_META_BYTES)
            self._meta.buf[:_JPEG_META_BYTES] = b"\x00" * _JPEG_META_BYTES
            self._owner = True
        else:
            shm_name, meta_name = _attach
            self._shm = shared_memory.SharedMemory(name=shm_name)
            self._meta = shared_memory.SharedMemory(name=meta_name)
            self._owner = False

    # -- pickling ------------------------------------------------------

    def __getstate__(self) -> dict:
        return {
            "cam_id": self.cam_id,
            "max_size": self.max_size,
            "shm": self._shm.name,
            "meta": self._meta.name,
        }

    def __setstate__(self, state: dict) -> None:
        self.__init__(
            state["cam_id"],
            state["max_size"],
            _attach=(state["shm"], state["meta"]),
        )

    # -- producer / consumer ------------------------------------------

    def write(self, jpeg: bytes, ts: float) -> bool:
        n = len(jpeg)
        if n == 0 or n > self.max_size:
            return False
        ts_int = int(ts * 1_000_000)

        ver = struct.unpack_from("<q", self._meta.buf, 16)[0]
        struct.pack_into("<q", self._meta.buf, 16, ver + 1)  # odd
        self._shm.buf[:n] = jpeg
        struct.pack_into("<q", self._meta.buf, 0, n)
        struct.pack_into("<q", self._meta.buf, 8, ts_int)
        struct.pack_into("<q", self._meta.buf, 16, ver + 2)  # even
        return True

    def read(self) -> tuple[Optional[bytes], float]:
        for _ in range(_SEQLOCK_MAX_RETRIES):
            v1 = struct.unpack_from("<q", self._meta.buf, 16)[0]
            if v1 & 1:
                continue
            n = struct.unpack_from("<q", self._meta.buf, 0)[0]
            ts_int = struct.unpack_from("<q", self._meta.buf, 8)[0]
            if n == 0:
                return None, 0.0
            data = bytes(self._shm.buf[:n])
            v2 = struct.unpack_from("<q", self._meta.buf, 16)[0]
            if v1 != v2:
                continue
            return data, ts_int / 1_000_000.0
        return None, 0.0

    def close(self) -> None:
        for shm in (self._shm, self._meta):
            try:
                shm.close()
            except Exception:
                pass
            if self._owner:
                try:
                    shm.unlink()
                except Exception:
                    pass


# --------------------------------------------------------------------------
# Registries — owner-side bookkeeping
# --------------------------------------------------------------------------

class FrameBufferRegistry:
    """Owns FrameSlot lifecycle. Workers receive the dict at start time."""

    def __init__(self, max_h: int, max_w: int):
        self._max_h = max_h
        self._max_w = max_w
        self._slots: dict[str, FrameSlot] = {}

    def register(self, cam_id: str) -> FrameSlot:
        slot = self._slots.get(cam_id)
        if slot is None:
            slot = FrameSlot(cam_id, self._max_h, self._max_w)
            self._slots[cam_id] = slot
        return slot

    def get(self, cam_id: str) -> Optional[FrameSlot]:
        return self._slots.get(cam_id)

    def unregister(self, cam_id: str) -> None:
        slot = self._slots.pop(cam_id, None)
        if slot is not None:
            slot.close()

    def snapshot(self) -> dict[str, FrameSlot]:
        return dict(self._slots)

    def shutdown(self) -> None:
        for slot in list(self._slots.values()):
            slot.close()
        self._slots.clear()


class JPEGBufferRegistry:
    """Owns JPEGSlot lifecycle for the latest annotated preview per camera."""

    def __init__(self, max_size: int = 2 * 1024 * 1024):
        self._max_size = max_size
        self._slots: dict[str, JPEGSlot] = {}

    def register(self, cam_id: str) -> JPEGSlot:
        slot = self._slots.get(cam_id)
        if slot is None:
            slot = JPEGSlot(cam_id, self._max_size)
            self._slots[cam_id] = slot
        return slot

    def get(self, cam_id: str) -> Optional[JPEGSlot]:
        return self._slots.get(cam_id)

    def unregister(self, cam_id: str) -> None:
        slot = self._slots.pop(cam_id, None)
        if slot is not None:
            slot.close()

    def snapshot(self) -> dict[str, JPEGSlot]:
        return dict(self._slots)

    def shutdown(self) -> None:
        for slot in list(self._slots.values()):
            slot.close()
        self._slots.clear()
