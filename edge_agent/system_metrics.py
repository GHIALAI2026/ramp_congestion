from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

import psutil

from edge_agent import config as cfg

logger = logging.getLogger(__name__)

_NULL_RE = re.compile(r"\x00+")


def _command_exists(binary: str) -> bool:
    return os.path.exists(binary) if os.path.isabs(binary) else shutil.which(binary) is not None


def _candidate_commands(
    base_cmd: list[str],
    *,
    preserve_env: bool = False,
    prefer_sudo: bool = False,
) -> list[list[str]]:
    commands: list[list[str]] = []
    sudo = shutil.which("sudo")
    commands.append(base_cmd)
    if sudo:
        sudo_cmd = [sudo, "-n"]
        if preserve_env:
            sudo_cmd.append("-E")
        if prefer_sudo:
            commands.insert(0, sudo_cmd + base_cmd)
        else:
            commands.append(sudo_cmd + base_cmd)
    return commands


def _clean_text(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode(errors="ignore")
    return _NULL_RE.sub("", text).strip()


def _merge_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    return _clean_text(_clean_text(stdout) + "\n" + _clean_text(stderr))


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    depth = 0
    start: int | None = None
    for idx, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                snippet = text[start:idx + 1]
                try:
                    objects.append(json.loads(snippet))
                except json.JSONDecodeError:
                    pass
                start = None
    return objects


def _last_error(errors: list[str], fallback: str) -> str:
    return errors[-1] if errors else fallback


class SystemMetricsCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._axsystemserver_proc: subprocess.Popen[str] | None = None
        self._last_sample_ts = 0.0
        self._last_sample: dict[str, Any] = {}
        self._axelera_name = "Axelera Metis"
        psutil.cpu_percent(interval=None)

    def close(self) -> None:
        with self._lock:
            proc = self._axsystemserver_proc
            self._axsystemserver_proc = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def collect(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._last_sample and (now - self._last_sample_ts) < cfg.SYSTEM_METRICS_CACHE_S:
                return dict(self._last_sample)

            sample: dict[str, Any] = {
                "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
                "mem_pct": round(psutil.virtual_memory().percent, 1),
            }
            sample.update(self._collect_intel_decode_gpu())
            sample.update(self._collect_axelera_usage())
            self._last_sample = sample
            self._last_sample_ts = now
            return dict(sample)

    def _collect_intel_decode_gpu(self) -> dict[str, Any]:
        base_cmd = [
            cfg.INTEL_GPU_TOP_BIN,
            "-J",
            "-s",
            str(cfg.INTEL_GPU_TOP_SAMPLE_MS),
            "-o",
            "-",
        ]
        if not _command_exists(base_cmd[0]):
            return {
                "igpu_name": "Intel decode GPU",
                "igpu_note": "intel_gpu_top not found",
            }

        errors: list[str] = []
        for cmd in _candidate_commands(base_cmd):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=cfg.INTEL_GPU_TOP_TIMEOUT_S,
                    check=False,
                )
                output = _clean_text(result.stdout or result.stderr)
                if result.returncode != 0 and not output:
                    errors.append(f"{' '.join(cmd[:2])}: exit {result.returncode}")
                    continue
            except subprocess.TimeoutExpired as exc:
                output = _merge_output(exc.stdout, exc.stderr)
            except Exception as exc:
                errors.append(str(exc))
                continue

            parsed = self._parse_intel_gpu_output(output)
            if parsed:
                return parsed
            if output:
                errors.append(output.splitlines()[-1])

        return {
            "igpu_name": "Intel decode GPU",
            "igpu_note": _last_error(errors, "intel_gpu_top output unavailable"),
        }

    def _parse_intel_gpu_output(self, output: str) -> dict[str, Any]:
        samples = _extract_json_objects(output)
        for sample in reversed(samples):
            engines = sample.get("engines") or {}
            if not isinstance(engines, dict):
                continue
            video_engines = []
            enhance_engines = []
            for engine_name, stats in engines.items():
                if not isinstance(stats, dict):
                    continue
                busy = float(stats.get("busy") or 0.0)
                name = str(engine_name).lower()
                if "videoenhance" in name or "vecs" in name:
                    enhance_engines.append(busy)
                elif "video" in name or "vcs" in name:
                    video_engines.append(busy)

            video_busy = sum(video_engines) / len(video_engines) if video_engines else 0.0
            enhance_busy = sum(enhance_engines) / len(enhance_engines) if enhance_engines else 0.0

            actual_freq = sample.get("frequency", {}).get("actual")
            note = "intel_gpu_top"
            if actual_freq not in (None, ""):
                try:
                    note = f"intel_gpu_top @ {float(actual_freq):.0f} MHz"
                except (TypeError, ValueError):
                    pass

            return {
                "igpu_name": "Intel iGPU",
                "igpu_pct": round(min(100.0, video_busy), 1),
                "igpu_video_pct": round(min(100.0, video_busy), 1),
                "igpu_video_enhance_pct": round(min(100.0, enhance_busy), 1),
                "igpu_note": note,
            }
        return {}

    def _collect_axelera_usage(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {"gpu_name": self._axelera_name}
        note = self._ensure_axsystemserver()
        if note:
            metrics["gpu_note"] = note
            return metrics

        base_cmd = [cfg.AXMONITOR_BIN, "--ui", "console"]
        if not _command_exists(base_cmd[0]):
            metrics["gpu_note"] = "axmonitor not found"
            return metrics

        errors: list[str] = []
        for cmd in _candidate_commands(base_cmd, preserve_env=True):
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                out, _ = proc.communicate("print\nexit\n", timeout=cfg.AXMONITOR_TIMEOUT_S)
                output = _clean_text(out)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                output = _merge_output(exc.stdout, exc.stderr)
            except Exception as exc:
                errors.append(str(exc))
                continue

            parsed = self._parse_axmonitor_output(output)
            if parsed:
                metrics.update(parsed)
                return metrics
            if output:
                errors.append(output.splitlines()[-1])

        metrics["gpu_note"] = _last_error(errors, "axmonitor output unavailable")
        return metrics

    def _parse_axmonitor_output(self, output: str) -> dict[str, Any]:
        lines = [_clean_text(line) for line in output.splitlines()]
        cores: list[dict[str, Any]] = []
        contexts: list[dict[str, Any]] = []
        board_temps: list[float] = []
        total_ddr_mb: float | None = None
        used_ddr_mb: float | None = None

        section: str | None = None
        current_core: dict[str, Any] | None = None
        current_context: dict[str, Any] | None = None

        for line in lines:
            if not line or line in {"print", "exit", "Closing App...", "Exiting CLI..."}:
                continue
            if line.startswith("axmonitor> Welcome") or line == "axmonitor>":
                continue
            if line.startswith("# Device"):
                section = "device"
                current_core = None
                current_context = None
                continue
            core_match = re.match(r"Core (\d+):$", line)
            if core_match:
                current_core = {"index": int(core_match.group(1))}
                cores.append(current_core)
                current_context = None
                section = "core"
                continue
            context_match = re.match(r"Context (\d+):$", line)
            if context_match:
                current_context = {"index": int(context_match.group(1))}
                contexts.append(current_context)
                current_core = None
                section = "context"
                continue
            if line.startswith("Board Sensor"):
                current_core = None
                current_context = None
                section = "board"
                continue
            if line.startswith("Memory:"):
                current_core = None
                current_context = None
                section = "memory"
                continue
            if line.startswith("PCIe DMA Info:"):
                current_core = None
                current_context = None
                section = "pcie"
                continue
            if line.startswith("Processes:"):
                current_core = None
                current_context = None
                section = "processes"
                continue

            if section == "core" and current_core is not None:
                if match := re.match(r"Core Utilization:\s*([0-9.]+)%$", line):
                    current_core["util"] = float(match.group(1))
                elif match := re.match(r"Temperature:\s*([0-9.]+)°C$", line):
                    current_core["temp_c"] = float(match.group(1))
                elif match := re.match(r"Clock Frequency:\s*([0-9.]+) MHz$", line):
                    current_core["clock_mhz"] = float(match.group(1))
                elif match := re.match(r"Num Kernels:\s*(\d+)$", line):
                    current_core["kernels"] = int(match.group(1))
                elif match := re.match(r"Total Runtime:\s*([0-9.]+) us$", line):
                    current_core["runtime_us"] = float(match.group(1))
                continue

            if section == "board":
                if match := re.match(r"Temperature:\s*([0-9.]+)°C$", line):
                    board_temps.append(float(match.group(1)))
                continue

            if section == "context" and current_context is not None:
                if match := re.match(r"DDR Utilization:\s*([0-9.]+) MB$", line):
                    current_context["ddr_mb"] = float(match.group(1))
                elif match := re.match(r"Is Active:\s*(Yes|No)$", line):
                    current_context["active"] = match.group(1) == "Yes"
                continue

            if section == "memory":
                if match := re.match(r"Total DDR Size:\s*([0-9.]+) MB$", line):
                    total_ddr_mb = float(match.group(1))
                elif match := re.match(r"Used DDR Size:\s*([0-9.]+) MB$", line):
                    used_ddr_mb = float(match.group(1))

        if not cores:
            return {}

        utilizations = [float(core.get("util") or 0.0) for core in cores]
        total_cores = len(cores)
        active_cores = sum(
            1 for core in cores if (core.get("util") or 0) > 0 or (core.get("kernels") or 0) > 0
        )
        temps = board_temps or [float(core["temp_c"]) for core in cores if "temp_c" in core]
        clocks = [float(core["clock_mhz"]) for core in cores if "clock_mhz" in core]
        mem_pct = None
        if total_ddr_mb and used_ddr_mb is not None:
            mem_pct = round(min(100.0, (used_ddr_mb / total_ddr_mb) * 100.0), 1)

        note_parts = ["axmonitor"]
        if total_cores:
            note_parts.append(f"{active_cores}/{total_cores} cores active")
        if clocks:
            note_parts.append(f"{max(clocks):.0f} MHz")

        return {
            "gpu_pct": round(sum(utilizations) / total_cores, 1),
            "gpu_mem_pct": mem_pct,
            "gpu_temp_c": round(max(temps), 1) if temps else None,
            "gpu_mem_used_mb": round(used_ddr_mb, 1) if used_ddr_mb is not None else None,
            "gpu_mem_total_mb": round(total_ddr_mb, 1) if total_ddr_mb is not None else None,
            "gpu_active_cores": active_cores,
            "gpu_total_cores": total_cores,
            "gpu_note": " · ".join(note_parts),
        }

    def _ensure_axsystemserver(self) -> str | None:
        if self._axsystemserver_running():
            return None

        base_cmd = [cfg.AXSYSTEMSERVER_BIN, "-l", "error"]
        if not _command_exists(base_cmd[0]):
            return "axsystemserver not found"

        errors: list[str] = []
        for cmd in _candidate_commands(base_cmd):
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                time.sleep(cfg.AXSYSTEMSERVER_STARTUP_S)
                if proc.poll() is None:
                    self._axsystemserver_proc = proc
                    return None
                output = _clean_text(proc.stdout.read() if proc.stdout else "")
                errors.append(output or f"{' '.join(cmd[:2])}: exit {proc.returncode}")
            except Exception as exc:
                errors.append(str(exc))

        return _last_error(errors, "Unable to start axsystemserver")

    def _axsystemserver_running(self) -> bool:
        proc = self._axsystemserver_proc
        if proc and proc.poll() is None:
            return True
        self._axsystemserver_proc = None

        for process in psutil.process_iter(["cmdline"]):
            try:
                cmdline = process.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if any("axsystemserver" in part for part in cmdline):
                return True
        return False
