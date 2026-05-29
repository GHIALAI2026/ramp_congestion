"""Lazy OpenCV decoder + cheap TCP preflight for RTSP sources.

The decoder is only spun up when Voyager has not produced a frame
recently — it does NOT run for the full lifetime of a camera. The
preflight is now a TCP connect to host:port (sub-second), not a full
RTSP open. Both changes drop tens of seconds off the startup path
when running 30 streams.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def tcp_preflight(source_url: str, timeout_s: float) -> bool:
    """Return True if we can open a TCP connection to the source's host:port.

    For RTSP this is the cheapest possible "is the source reachable"
    check — no RTSP handshake, no auth, no decode. Local files always
    pass.
    """
    if not source_url:
        return False
    try:
        parts = urlsplit(source_url)
    except Exception:
        return False
    if parts.scheme not in ("rtsp", "rtsps", "http", "https"):
        return True  # files / unknown schemes — let the decoder decide
    host = parts.hostname
    if not host:
        return False
    port = parts.port or (554 if parts.scheme.startswith("rtsp") else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def rtsp_preflight(
    source_url: str,
    tcp_timeout_s: float,
    codec_timeout_s: float,
) -> tuple[bool, str]:
    """Two-stage preflight: TCP reach, then ffprobe codec validation.

    A camera can pass TCP preflight (RTSP server listens on 554) while
    still streaming malformed video — broken H.265 PPS NALs, dead
    encoder, garbage SDP. Handing such a source to the Voyager SDK
    causes its GStreamer pipeline to wait tens of seconds for a valid
    keyframe, holding ``_deploy_lock`` and blocking every other
    camera's startup.

    This second stage runs ``ffprobe`` with a hard timeout and verifies
    the stream reports a positive width/height. Cameras that fail are
    routed to the background retry loop instead of the synchronous SDK
    path. Falls back to TCP-only if ffprobe isn't on PATH.

    Returns ``(healthy, reason)``.
    """
    if not tcp_preflight(source_url, tcp_timeout_s):
        return False, "tcp_unreachable"

    try:
        scheme = urlsplit(source_url).scheme
    except Exception:
        return True, "ok"
    if scheme not in ("rtsp", "rtsps"):
        return True, "ok"

    if not shutil.which("ffprobe"):
        return True, "ok_ffprobe_unavailable"

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-rtsp_transport", "tcp",
                "-timeout", str(int(codec_timeout_s * 1_000_000)),
                "-i", source_url,
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
            ],
            capture_output=True, text=True,
            timeout=codec_timeout_s + 1.0,
        )
    except subprocess.TimeoutExpired:
        return False, "codec_probe_timeout"
    except OSError as e:
        return False, f"codec_probe_error:{e}"

    if result.returncode != 0:
        return False, "codec_probe_failed"
    lines = (result.stdout or "").strip().splitlines()
    if not lines:
        return False, "codec_no_streams"
    try:
        w_str, h_str = lines[0].split(",")[:2]
        w, h = int(w_str), int(h_str)
    except (ValueError, IndexError):
        return False, "codec_params_unparseable"
    if w <= 0 or h <= 0:
        return False, f"codec_params_invalid({w}x{h})"
    return True, "ok"


class VideoDecoder:
    """Background OpenCV decoder with lazy startup and reconnect.

    Stays dormant until ``start()`` is called. The pipeline only calls
    start() when Voyager has not delivered a fresh frame for the camera;
    once Voyager catches up the decoder can be paused via stop().
    """

    def __init__(self, source_url: str, camera_id: str = "unknown"):
        self._source = source_url
        self._camera_id = camera_id
        self._is_file = not source_url.startswith("rtsp://")

        self._latest_frame: Optional[np.ndarray] = None
        self._latest_lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._decode_loop,
            daemon=True,
            name=f"FallbackDecoder-{self._camera_id}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._ready_event.clear()

    def get_snapshot(self) -> Optional[np.ndarray]:
        with self._latest_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def wait_until_ready(self, timeout_s: float) -> bool:
        if self.get_snapshot() is not None:
            return True
        return self._ready_event.wait(timeout=max(0.0, timeout_s))

    def _decode_loop(self) -> None:
        backoff = 1.0
        while self._running:
            cap = self._open()
            if cap is None:
                logger.warning(
                    "[%s] Fallback decoder could not open %s, retrying in %.0fs",
                    self._camera_id, self._source, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            logger.info("[%s] Fallback decoder opened %s",
                        self._camera_id, self._source)
            backoff = 1.0

            while self._running:
                ok, frame = cap.read()
                if not ok:
                    if self._is_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    logger.warning(
                        "[%s] Fallback decoder read failed, reconnecting",
                        self._camera_id,
                    )
                    break

                with self._latest_lock:
                    self._latest_frame = frame
                self._ready_event.set()

            cap.release()

    def _open(self) -> Optional[cv2.VideoCapture]:
        if self._is_file:
            cap = cv2.VideoCapture(self._source)
        else:
            pipeline = (
                f"uridecodebin uri={self._source} source::latency=0 ! "
                "videoconvert ! video/x-raw, format=BGR ! appsink drop=true"
            )
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if not cap.isOpened():
                cap = cv2.VideoCapture(self._source, cv2.CAP_GSTREAMER)
            if not cap.isOpened():
                cap = cv2.VideoCapture(self._source, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            return None
        return cap
