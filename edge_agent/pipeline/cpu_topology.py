"""CPU topology detection and thread/process pinning.

Intel hybrid CPUs (12th-gen+, Ultra series) have Performance (P) cores
and Efficiency (E) cores on the same package. The Linux scheduler is
reactive — for a thread that runs continuously (the Voyager iter
thread, analytics shards) we want a fixed core so the L1/L2 cache
stays warm and the thread doesn't bounce between P and E cores.

This module:
  * detects P/E core sets from /sys/devices/system/cpu (cpufreq max),
  * exposes pinning primitives (pin_current_thread, pin_process),
  * builds a CPULayout that assigns roles to cores.

Falls back gracefully on non-hybrid CPUs, containers without /sys
access, or non-Linux platforms — the helpers become no-ops and the
agent runs as before.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_SYSFS_CPU = Path("/sys/devices/system/cpu")


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def detect_topology() -> tuple[list[int], list[int]]:
    """Return (p_cores, e_cores) as sorted lists of logical CPU IDs.

    Detection rule: read each cpu's ``cpufreq/cpuinfo_max_freq``. If we
    see more than one distinct max frequency, the highest group is P,
    everything else is E. If we see only one group, treat them all as
    P (so the layout still works on homogeneous CPUs).
    """
    if not _SYSFS_CPU.exists():
        return _all_cores_as_p()

    freqs: dict[int, int] = {}
    for cpu_dir in sorted(_SYSFS_CPU.glob("cpu[0-9]*")):
        try:
            cpu_id = int(cpu_dir.name[3:])
        except ValueError:
            continue
        f = _read_int(cpu_dir / "cpufreq" / "cpuinfo_max_freq")
        if f is not None:
            freqs[cpu_id] = f

    if not freqs:
        return _all_cores_as_p()

    distinct = sorted(set(freqs.values()))
    if len(distinct) == 1:
        return sorted(freqs), []

    p_freq = distinct[-1]
    p_cores = sorted(c for c, f in freqs.items() if f == p_freq)
    e_cores = sorted(c for c, f in freqs.items() if f != p_freq)
    return p_cores, e_cores


def _all_cores_as_p() -> tuple[list[int], list[int]]:
    n = os.cpu_count() or 1
    return list(range(n)), []


def pin_current_thread(cpu_set: set[int]) -> bool:
    """Pin the current thread to ``cpu_set``. Returns True on success.

    Linux affinity is per-task, so calling this from within a thread
    pins that thread only. No-op on platforms without sched_setaffinity.
    """
    if not cpu_set:
        return False
    try:
        os.sched_setaffinity(0, cpu_set)
        return True
    except (OSError, AttributeError):
        return False


def pin_process(pid: int, cpu_set: set[int]) -> bool:
    """Pin a process by pid to ``cpu_set``. Returns True on success."""
    if not cpu_set:
        return False
    try:
        os.sched_setaffinity(pid, cpu_set)
        return True
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Layout planner
# ---------------------------------------------------------------------------

class CPULayout:
    """Pre-computed core assignments.

    Strategy:
      * iter thread → P-core[0]   (single core, never migrates)
      * shards     → P-cores[1:]  (one per shard, round-robin)
      * GST/Voyager threads → remaining P-cores (Voyager has its own pool)
      * UI/HTTP/MQTT/heartbeat → E-cores (or all P if no E-cores)
    """

    def __init__(
        self,
        iter_core: Optional[int],
        shard_cores: list[int],
        worker_cores: list[int],
        gst_cores: list[int],
        topology_detected: bool,
    ):
        self.iter_core = iter_core
        self.shard_cores = list(shard_cores)
        self.worker_cores = list(worker_cores)
        self.gst_cores = list(gst_cores)
        self.topology_detected = topology_detected

    @property
    def iter_set(self) -> set[int]:
        return {self.iter_core} if self.iter_core is not None else set()

    def shard_set(self, shard_idx: int) -> set[int]:
        if not self.shard_cores:
            return set()
        return {self.shard_cores[shard_idx % len(self.shard_cores)]}

    @property
    def worker_set(self) -> set[int]:
        return set(self.worker_cores)


def plan_layout(num_shards: int) -> CPULayout:
    """Compute a sensible CPU-role assignment for ``num_shards`` shards."""
    p_cores, e_cores = detect_topology()
    detected = bool(p_cores and e_cores)

    if not p_cores:
        logger.info("CPU pinning: no topology info; running unpinned.")
        return CPULayout(None, [], [], [], False)

    iter_core = p_cores[0]
    remaining_p = p_cores[1:]
    shard_cores = remaining_p[:num_shards] if num_shards else []
    gst_cores = remaining_p[len(shard_cores):]

    # Spill onto E-cores if we have more shards than spare P-cores.
    leftover_e = list(e_cores)
    if num_shards and len(shard_cores) < num_shards and leftover_e:
        spill = num_shards - len(shard_cores)
        shard_cores += leftover_e[:spill]
        leftover_e = leftover_e[spill:]

    worker_cores = leftover_e if leftover_e else p_cores

    logger.info(
        "CPU layout — detected=%s, iter=%s, shards=%s, gst=%s, workers=%s",
        detected, iter_core, shard_cores, gst_cores, worker_cores,
    )
    return CPULayout(iter_core, shard_cores, worker_cores, gst_cores, detected)
