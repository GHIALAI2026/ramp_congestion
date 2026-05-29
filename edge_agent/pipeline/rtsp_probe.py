"""Probe RTSP cameras for their declared frame rate via ffprobe.

Used by the dashboard cameras page to show the camera's actual stream
fps rather than the post-inference fps (which is gated to TARGET_FPS).

The result for a given source URL is cached for the lifetime of the
edge process — RTSP camera frame rates are a hardware/firmware setting
that doesn't change at runtime. Probes run on a tiny background
thread pool so the first /camera_stats request doesn't block while N
ffprobe subprocesses warm up.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

_FFPROBE_BIN = shutil.which("ffprobe") or "/opt/ffmpeg/ffprobe"
_PROBE_TIMEOUT_S = 8.0

_cache_lock = threading.Lock()
_cache: dict[str, Optional[float]] = {}
_inflight: set[str] = set()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rtsp-probe")


def _parse_rate(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s or s == "0/0":
        return None
    if "/" in s:
        num, _, den = s.partition("/")
        try:
            n, d = float(num), float(den)
            if d > 0:
                return n / d
        except ValueError:
            return None
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _probe(source_url: str) -> Optional[float]:
    """Blocking ffprobe call. Returns nominal stream fps, or None on failure."""
    cmd = [
        _FFPROBE_BIN,
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=0",
        source_url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("ffprobe failed for %s: %s", source_url, exc)
        return None

    if result.returncode != 0:
        logger.debug("ffprobe rc=%s for %s: %s",
                     result.returncode, source_url, result.stderr.strip())
        return None

    # Prefer avg_frame_rate (camera-published), fall back to r_frame_rate.
    avg = r = None
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "avg_frame_rate":
            avg = _parse_rate(value)
        elif key == "r_frame_rate":
            r = _parse_rate(value)
    return avg or r


def _probe_and_cache(source_url: str) -> None:
    fps = _probe(source_url)
    with _cache_lock:
        _cache[source_url] = fps
        _inflight.discard(source_url)
    logger.info("rtsp probe %s → %s fps", source_url, fps)


def get_rtsp_fps(source_url: str) -> Optional[float]:
    """Return cached fps for ``source_url``; kick off a probe if uncached.

    Non-blocking — returns None until the background probe completes.
    Subsequent calls after the probe finishes return the cached value.
    """
    if not source_url:
        return None
    with _cache_lock:
        if source_url in _cache:
            return _cache[source_url]
        if source_url in _inflight:
            return None
        _inflight.add(source_url)
    try:
        _executor.submit(_probe_and_cache, source_url)
    except RuntimeError:
        # executor shutting down; remove the inflight marker so a future
        # call can retry if we come back up
        with _cache_lock:
            _inflight.discard(source_url)
    return None


def invalidate(source_url: str) -> None:
    """Drop the cached fps so the next call re-probes (e.g. on stream errors)."""
    with _cache_lock:
        _cache.pop(source_url, None)
        _inflight.discard(source_url)
