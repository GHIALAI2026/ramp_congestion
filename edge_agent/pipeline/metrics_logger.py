"""Per-second throughput logger.

Writes one line per second to a dedicated file with cumulative counters
and per-second deltas for frames inferenced (returned by Metis) and
frames tracked (processed by OC-SORT). The file is truncated at startup
and appended to thereafter.

Format:
    <iso8601> metrics inferenced_per_s=<n> tracked_per_s=<n>
        dropped_shard_per_s=<n> inferenced_total=<n> tracked_total=<n>
        cams_active=<n>

`dropped_shard_per_s` is the gap between Metis output and OC-SORT input
in the last second — non-zero means the analytics shard queue dropped
wakeup tokens under load.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


SnapshotFn = Callable[[], dict]


class ThroughputLogger:
    def __init__(
        self,
        log_path: str | Path,
        snapshot_fn: SnapshotFn,
        interval_s: float = 1.0,
    ):
        self._log_path = Path(log_path)
        self._snapshot_fn = snapshot_fn
        self._interval_s = max(0.1, float(interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fh = None
        self._last: dict[str, int] = {}

    def start(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # mode="w" truncates on open; line-buffered so each tick flushes.
        self._fh = open(self._log_path, "w", buffering=1)
        self._fh.write(
            "# columns: ts metrics inferenced_per_s tracked_per_s "
            "dropped_shard_per_s inferenced_total tracked_total cams_active\n"
        )
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="throughput-logger",
        )
        self._thread.start()
        logger.info("ThroughputLogger writing to %s", self._log_path)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._emit(self._snapshot_fn())
            except Exception:
                logger.exception("throughput emit failed")

    def _emit(self, snapshot: dict) -> None:
        if self._fh is None:
            return
        inferenced = int(snapshot.get("inferenced", 0))
        tracked = int(snapshot.get("tracked", 0))
        cams_active = int(snapshot.get("cams_active", 0))

        d_inf = inferenced - self._last.get("inferenced", 0)
        d_trk = tracked - self._last.get("tracked", 0)
        # Gap = wakeup tokens lost to a full shard queue this second.
        d_drop = max(0, d_inf - d_trk)

        self._last["inferenced"] = inferenced
        self._last["tracked"] = tracked

        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._fh.write(
            f"{ts} metrics inferenced_per_s={d_inf} tracked_per_s={d_trk} "
            f"dropped_shard_per_s={d_drop} inferenced_total={inferenced} "
            f"tracked_total={tracked} cams_active={cams_active}\n"
        )
