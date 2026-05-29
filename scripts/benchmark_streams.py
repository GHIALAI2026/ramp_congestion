#!/usr/bin/env python3
"""Edge benchmark: scale 20 → 30 → 35 → 40 → 45 streams.

For each case, sample for SAMPLE_S seconds, then write a Markdown report
covering pipeline performance, hardware utilization, and data integrity.

Run with:
    python3 scripts/benchmark_streams.py

Adds bench_cam_* rows to the cameras table while running, then deletes
them at the end. Requires the stack already up (start.sh) with the
VOYAGER_MAX_SOURCES_PER_PIPELINE=12 env var so 45 streams fit.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from statistics import mean, median, stdev
from typing import Optional

import httpx
import paho.mqtt.client as mqtt


ROOT = Path("/home/admin1/Vehicle_Detection_Git/Vehicle_Detection")
LOG_DIR = ROOT / ".logs"
BENCH_DIR = ROOT / "bench_results"
BENCH_DIR.mkdir(exist_ok=True)

API = "http://localhost:8002"
EDGE_HTTP = "http://localhost:8003"
EDGE_ID = "vehicle-edge-01"

CASES = [20, 30, 35, 40, 45]
SAMPLE_S = 180          # 3 minutes per case
SAMPLE_INTERVAL_S = 5   # one sample every 5 s
WARMUP_S = 45           # let new streams attach + producers stabilise

NEW_URLS = [
    "rtsp://kspoc:kspoc123@10.64.0.11/stream1",
    "rtsp://admin:Ap0%28%402024@10.86.95.22/videoStreamId=2",
    "rtsp://kspoc:kspoc123@10.64.0.13/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.14/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.15/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.16/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.30/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.31/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.33/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.34/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.36/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.37/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.38/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.40/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.41/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.42/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.44/stream1",
    "rtsp://kspoc:kspoc123@10.64.0.46/stream1",
    "rtsp://kspoc:kspoc123@10.86.158.163/rtsp/defaultPrimary?streamType=u",
    "rtsp://kspoc:kspoc123@10.86.158.196/rtsp/defaultPrimary?streamType=u",
    "rtsp://kspoc:kspoc123@10.86.158.168/rtsp/defaultPrimary?streamType=u",
    "rtsp://kspoc:kspoc123@10.86.158.140/rtsp/defaultPrimary?streamType=u",
    "rtsp://kspoc:kspoc123@10.86.158.172/rtsp/defaultPrimary?streamType=u",
    "rtsp://kspoc:kspoc123@10.86.158.71:554/videoStreamId=2",
    "rtsp://kspoc:kspoc123@10.86.158.179/rtsp/defaultPrimary?streamType=u",
]


# --------------------------------------------------------------------------
# Metric collectors (synchronous; each call is ~< 1 s)
# --------------------------------------------------------------------------

def sample_cpu_per_core() -> dict:
    """1-second mpstat sample; returns per-core busy% + global summary.

    Parses by reading the LAST 11 columns of each data row — they are
    fixed: CPU, %usr, %nice, %sys, %iowait, %irq, %soft, %steal, %guest,
    %gnice, %idle. The earlier indexing-from-the-front approach broke
    under 12-hour locales where the time prefix is `HH:MM:SS AM IST`
    (4 tokens) vs 24-hour (1 token), shifting the CPU column.
    """
    try:
        r = subprocess.run(
            ["mpstat", "-P", "ALL", "1", "1"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception as exc:
        return {"error": str(exc), "cores": {}}
    cores: dict[int, float] = {}
    overall = None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 11:
            continue
        tail = parts[-11:]
        cpu_field = tail[0]
        try:
            idle = float(tail[-1])
        except ValueError:
            continue
        if cpu_field == "all":
            overall = round(100 - idle, 1)
        elif cpu_field.isdigit():
            cores[int(cpu_field)] = round(100 - idle, 1)
    return {"overall_busy_pct": overall, "cores": cores}


def sample_mem() -> dict:
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            v = rest.strip().split()[0]
            try:
                out[k] = int(v)  # kB
            except ValueError:
                pass
    total = out.get("MemTotal", 0)
    avail = out.get("MemAvailable", 0)
    used = total - avail
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "available_mb": round(avail / 1024, 1),
        "used_pct": round(used / total * 100, 1) if total else 0,
    }


def sample_igpu(duration_s: float = 2.0) -> dict:
    """Run intel_gpu_top for ~duration_s and average engine busy %.

    Runs WITHOUT sudo — intel_gpu_top reads perf counters that this user
    has access to. Wrapped in subprocess.run with a hard timeout so a
    stuck child cannot wedge the benchmark. The earlier sudo+SIGTERM
    approach left orphan grandchildren that held the stdout pipe open
    and blocked communicate() forever.
    """
    period_ms = 500
    out = ""
    try:
        result = subprocess.run(
            ["intel_gpu_top", "-J", "-s", str(period_ms)],
            capture_output=True, text=True,
            timeout=duration_s + 0.5,
            start_new_session=True,
        )
        out = result.stdout or ""
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already SIGKILLed the child by the time this fires;
        # exc.stdout has whatever was captured before the kill.
        if exc.stdout:
            out = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return {"error": "intel_gpu_top not found"}
    except Exception as exc:
        return {"error": str(exc)}

    # intel_gpu_top -J emits a stream of JSON objects separated by commas
    # inside an outer array that never closes. Split on top-level objects.
    samples = _parse_intel_gpu_top_json(out)
    if not samples:
        return {"samples": 0}

    # Aggregate the named engines across samples (avg busy%).
    engine_totals: dict[str, list[float]] = {}
    for s in samples:
        for engine_name, engine_data in (s.get("engines") or {}).items():
            try:
                busy = float(engine_data.get("busy", 0))
            except (TypeError, ValueError):
                continue
            engine_totals.setdefault(engine_name, []).append(busy)
    engines_avg = {k: round(mean(v), 1) for k, v in engine_totals.items()}
    return {"samples": len(samples), "engines_busy_pct": engines_avg}


def _parse_intel_gpu_top_json(text: str) -> list[dict]:
    """intel_gpu_top -J output is `[\\n{...},\\n{...},\\n...` (no closing).
    Extract each top-level object by brace-matching."""
    samples: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                chunk = text[start:i + 1]
                try:
                    samples.append(json.loads(chunk))
                except json.JSONDecodeError:
                    pass
                start = -1
    return samples


def parse_throughput_lines(lines: list[str]) -> dict:
    """Parse a slice of throughput.log lines."""
    inferenced = []
    tracked = []
    dropped = []
    cams_active = []
    for line in lines:
        if "metrics" not in line:
            continue
        m_inf = re.search(r"inferenced_per_s=(\d+)", line)
        m_trk = re.search(r"tracked_per_s=(\d+)", line)
        m_drp = re.search(r"dropped_shard_per_s=(\d+)", line)
        m_cam = re.search(r"cams_active=(\d+)", line)
        if m_inf: inferenced.append(int(m_inf.group(1)))
        if m_trk: tracked.append(int(m_trk.group(1)))
        if m_drp: dropped.append(int(m_drp.group(1)))
        if m_cam: cams_active.append(int(m_cam.group(1)))
    return {
        "samples": len(inferenced),
        "inferenced_per_s_avg": round(mean(inferenced), 1) if inferenced else 0,
        "inferenced_per_s_min": min(inferenced) if inferenced else 0,
        "inferenced_per_s_max": max(inferenced) if inferenced else 0,
        "tracked_per_s_avg": round(mean(tracked), 1) if tracked else 0,
        "dropped_per_s_avg": round(mean(dropped), 2) if dropped else 0,
        "dropped_per_s_total": sum(dropped),
        "cams_active_avg": round(mean(cams_active), 1) if cams_active else 0,
        "cams_active_min": min(cams_active) if cams_active else 0,
        "cams_active_max": max(cams_active) if cams_active else 0,
    }


def grep_edge_log_errors(lines: list[str]) -> dict:
    pat = re.compile(r"\b(ERROR|CRITICAL|Traceback|fatal|stream_failure|mark_errored|FATAL)\b")
    matched = [l for l in lines if pat.search(l)]
    by_kind: dict[str, int] = {}
    for l in matched:
        kind = "other"
        if "ERROR" in l: kind = "ERROR"
        elif "CRITICAL" in l: kind = "CRITICAL"
        elif "Traceback" in l: kind = "Traceback"
        elif "mark_errored" in l: kind = "mark_errored"
        elif "stream_failure" in l: kind = "stream_failure"
        elif "fatal" in l.lower(): kind = "fatal"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"count": len(matched), "by_kind": by_kind, "head": matched[:10]}


# --------------------------------------------------------------------------
# Async helpers
# --------------------------------------------------------------------------

class HeartbeatWatcher:
    """Subscribes to the edge heartbeat MQTT topic and exposes the latest payload.

    Reads from the broker directly rather than from Redis (the cloud's MQTT
    consumer wires through Redis, but during diagnosis the
    ``vedge:{edge_id}:heartbeat`` key turned out to be empty even while the
    edge was publishing — this avoids that dependency entirely).
    """

    def __init__(self, edge_id: str = EDGE_ID) -> None:
        self._topic = f"vehicle/edge/{edge_id}/heartbeat"
        self._lock = threading.Lock()
        self._latest: dict = {}
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bench-hbwatch-{int(time.time())}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, props=None):
        client.subscribe(self._topic, qos=0)

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        with self._lock:
            self._latest = payload

    def start(self) -> None:
        self._client.connect("localhost", 1883, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest)


async def get_edge_heartbeat(watcher: "HeartbeatWatcher") -> dict:
    """Wrap the watcher's latest payload in float-coerced form for the sampler."""
    hb = watcher.latest()
    out: dict = {}
    for k, v in hb.items():
        if isinstance(v, (int, float)):
            out[k] = float(v)
        else:
            out[k] = v
    return out


async def get_camera_stats() -> dict:
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            r = await client.get(f"{EDGE_HTTP}/camera_stats")
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}


async def get_cameras_status() -> dict:
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            r = await client.get(f"{API}/api/cameras")
            cams = r.json() if r.status_code == 200 else []
        except Exception:
            return {"total": 0, "online": 0, "error": 0, "by_status": {}}
    by_status: dict[str, int] = {}
    for c in cams:
        s = c.get("status") or "unknown"
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "total": len(cams),
        "online": by_status.get("online", 0),
        "error": by_status.get("error", 0),
        "by_status": by_status,
    }


# --------------------------------------------------------------------------
# Camera CRUD
# --------------------------------------------------------------------------

async def add_camera(client: httpx.AsyncClient, cam_id: str, url: str) -> bool:
    try:
        r = await client.post(
            f"{API}/api/cameras",
            json={
                "camera_id": cam_id,
                "name": cam_id,
                "source_url": url,
                "assigned_edge": EDGE_ID,
            },
            timeout=10.0,
        )
        return r.status_code in (200, 201)
    except Exception as exc:
        print(f"  add_camera {cam_id} failed: {exc}")
        return False


async def delete_camera(client: httpx.AsyncClient, cam_id: str) -> bool:
    try:
        r = await client.delete(f"{API}/api/cameras/{cam_id}", timeout=10.0)
        return r.status_code in (200, 204)
    except Exception as exc:
        print(f"  delete_camera {cam_id} failed: {exc}")
        return False


async def list_bench_cameras(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(f"{API}/api/cameras", timeout=8.0)
    return sorted(c["camera_id"] for c in r.json() if c["camera_id"].startswith("bench_"))


async def scale_to(target: int, client: httpx.AsyncClient) -> tuple[int, int]:
    """Add bench cameras one at a time until camera count >= target.
    Returns (added, current_total)."""
    bench = await list_bench_cameras(client)
    cams = (await client.get(f"{API}/api/cameras")).json()
    current = len(cams)
    if current >= target:
        return (0, current)
    needed = target - current
    next_idx = (
        max(
            (int(c.rsplit("_", 1)[-1]) for c in bench if c.rsplit("_", 1)[-1].isdigit()),
            default=0,
        )
        + 1
    )
    added = 0
    pool_idx = next_idx - 1  # index into NEW_URLS
    while added < needed and pool_idx < len(NEW_URLS):
        cam_id = f"bench_cam_{next_idx:02d}"
        url = NEW_URLS[pool_idx]
        ok = await add_camera(client, cam_id, url)
        if ok:
            added += 1
            print(f"  + {cam_id} → {url[:60]}{'…' if len(url) > 60 else ''}", flush=True)
        next_idx += 1
        pool_idx += 1
    return (added, current + added)


def kick_edge_restart() -> None:
    """SIGKILL the edge so the supervisor in start.sh restarts it.

    The MQTT runtime-add path is broken (the listener's on_connect doesn't
    fire — separate bug, see TODO), so cameras added via the cloud API do
    not actually attach until the edge restarts and re-syncs from the DB.
    Restart guarantees a clean attach for every camera at each case.
    """
    print("  → SIGKILL edge so supervisor restarts with new camera set", flush=True)
    subprocess.run(
        ["pkill", "-9", "-f", "edge_agent.main"],
        capture_output=True, check=False,
    )


async def wait_for_edge_ready(target_cams: int, watcher: "HeartbeatWatcher",
                              timeout_s: float = 120.0) -> int:
    """Block until heartbeat reports cameras_active >= target_cams (or timeout).

    Returns the final cameras_active count. Polls the in-process MQTT watcher
    every 2 s. After timeout, returns whatever count was last observed (the
    benchmark proceeds anyway and the report flags the gap).
    """
    deadline = time.time() + timeout_s
    last = 0
    while time.time() < deadline:
        hb = watcher.latest()
        cams_active = int(hb.get("cameras_active") or 0)
        if cams_active > last:
            print(f"  → cameras_active={cams_active}/{target_cams}", flush=True)
            last = cams_active
        if cams_active >= target_cams:
            return cams_active
        await asyncio.sleep(2)
    print(f"  → timeout: stuck at {last}/{target_cams}", flush=True)
    return last


# --------------------------------------------------------------------------
# Per-case sampler
# --------------------------------------------------------------------------

async def run_case(case_target: int, client: httpx.AsyncClient, watcher: "HeartbeatWatcher") -> dict:
    print(f"\n=== Case: {case_target} streams ===", flush=True)
    added, total = await scale_to(case_target, client)
    print(f"  cloud DB total: {total} (added {added})", flush=True)

    # The MQTT runtime-add path doesn't deliver to the edge listener — work
    # around it by SIGKILLing the edge so the supervisor restarts it and the
    # boot path re-syncs cameras from the cloud DB.
    if added > 0:
        kick_edge_restart()

    # Wait for edge to attach the target count (or timeout). We give it more
    # rope on larger sets because pipeline assignment is serial.
    attach_timeout = 60 + total * 2
    actual_active = await wait_for_edge_ready(total, watcher, timeout_s=attach_timeout)

    # Mark log positions AFTER the edge is stable so we only analyse this case.
    throughput_path = LOG_DIR / "throughput.log"
    edge_log_path = LOG_DIR / "edge.log"
    throughput_start = throughput_path.stat().st_size if throughput_path.exists() else 0
    edge_log_start = edge_log_path.stat().st_size if edge_log_path.exists() else 0

    print(f"  warmup {WARMUP_S}s ...", flush=True)
    await asyncio.sleep(WARMUP_S)

    print(f"  sampling for {SAMPLE_S}s (every {SAMPLE_INTERVAL_S}s) ...", flush=True)
    samples: list[dict] = []
    started_at = time.time()
    while time.time() - started_at < SAMPLE_S:
        sample_t0 = time.time()
        cpu = sample_cpu_per_core()
        mem = sample_mem()
        # iGPU sample takes ~2 s; we run it every other sample to keep wall-clock close to SAMPLE_INTERVAL_S
        igpu = sample_igpu(duration_s=2.0) if len(samples) % 2 == 0 else None
        hb = await get_edge_heartbeat(watcher)
        cam_status = await get_cameras_status()
        samples.append({
            "ts": time.time(),
            "cpu": cpu,
            "mem": mem,
            "igpu": igpu,
            "heartbeat": hb,
            "cam_status": cam_status,
        })
        elapsed = time.time() - sample_t0
        sleep_for = max(0.0, SAMPLE_INTERVAL_S - elapsed)
        await asyncio.sleep(sleep_for)

    # Read appended throughput + edge.log slices
    throughput_tail = ""
    if throughput_path.exists():
        with open(throughput_path, "rb") as f:
            f.seek(throughput_start)
            throughput_tail = f.read().decode("utf-8", errors="replace")
    edge_log_tail = ""
    if edge_log_path.exists():
        with open(edge_log_path, "rb") as f:
            f.seek(edge_log_start)
            edge_log_tail = f.read().decode("utf-8", errors="replace")

    throughput_summary = parse_throughput_lines(throughput_tail.splitlines())
    integrity = grep_edge_log_errors(edge_log_tail.splitlines())

    # Aggregate sampled CPU / memory / iGPU / heartbeat
    def all_vals(getter):
        out = []
        for s in samples:
            try:
                v = getter(s)
                if v is not None:
                    out.append(float(v))
            except (TypeError, ValueError, KeyError):
                pass
        return out

    cpu_overall = all_vals(lambda s: s["cpu"].get("overall_busy_pct"))
    mem_used_pct = all_vals(lambda s: s["mem"]["used_pct"])
    mem_used_mb = all_vals(lambda s: s["mem"]["used_mb"])
    hb_inf_fps = all_vals(lambda s: s["heartbeat"].get("inference_fps"))
    hb_inf_ms = all_vals(lambda s: s["heartbeat"].get("inference_ms"))
    hb_cpu = all_vals(lambda s: s["heartbeat"].get("cpu_pct"))
    hb_mem = all_vals(lambda s: s["heartbeat"].get("mem_pct"))
    hb_cams_active = all_vals(lambda s: s["heartbeat"].get("cameras_active"))
    hb_cams_errored = all_vals(lambda s: s["heartbeat"].get("cameras_errored"))

    # Per-core average across samples (only cores that appeared)
    per_core: dict[int, list[float]] = {}
    for s in samples:
        for core, busy in (s["cpu"].get("cores") or {}).items():
            per_core.setdefault(int(core), []).append(float(busy))
    per_core_avg = {c: round(mean(v), 1) for c, v in per_core.items()}
    per_core_max = {c: round(max(v), 1) for c, v in per_core.items()}

    # iGPU engine averages across all iGPU samples
    igpu_engine_busy: dict[str, list[float]] = {}
    for s in samples:
        ig = s.get("igpu") or {}
        for k, v in (ig.get("engines_busy_pct") or {}).items():
            igpu_engine_busy.setdefault(k, []).append(float(v))
    igpu_engines_avg = {k: round(mean(v), 1) for k, v in igpu_engine_busy.items()}

    # Camera stats — final snapshot
    final_cam_stats = await get_camera_stats()

    def _stat(vals: list[float]) -> dict:
        if not vals:
            return {"avg": None, "min": None, "max": None, "p95": None, "n": 0}
        s = sorted(vals)
        p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
        return {
            "avg": round(mean(vals), 2),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "p95": round(p95, 2),
            "n": len(vals),
        }

    summary = {
        "target_streams": case_target,
        "scaled_total_at_start": total,
        "edge_active_at_sample_start": actual_active,
        "duration_s": round(time.time() - started_at, 1),
        "sample_count": len(samples),
        "cpu_overall_busy_pct": _stat(cpu_overall),
        "cpu_per_core_avg": per_core_avg,
        "cpu_per_core_peak": per_core_max,
        "mem_used_pct": _stat(mem_used_pct),
        "mem_used_mb": _stat(mem_used_mb),
        "igpu_engines_avg": igpu_engines_avg,
        "edge_heartbeat": {
            "inference_fps": _stat(hb_inf_fps),
            "inference_ms": _stat(hb_inf_ms),
            "cpu_pct": _stat(hb_cpu),
            "mem_pct": _stat(hb_mem),
            "cameras_active": _stat(hb_cams_active),
            "cameras_errored": _stat(hb_cams_errored),
        },
        "throughput": throughput_summary,
        "data_integrity": integrity,
        "final_camera_stats_sample": dict(list(final_cam_stats.items())[:3]),
        "final_camera_count": len(final_cam_stats),
    }
    return summary


# --------------------------------------------------------------------------
# Markdown report writer
# --------------------------------------------------------------------------

def write_markdown(summaries: list[dict], out_path: Path, started_at: dt.datetime) -> None:
    lines: list[str] = []
    A = lines.append

    A(f"# Edge Stream Benchmark — {started_at.strftime('%Y-%m-%d %H:%M %Z')}")
    A("")
    A(f"- Edge ID: `{EDGE_ID}`")
    A(f"- Stream counts tested: {', '.join(str(s['target_streams']) for s in summaries)}")
    A(f"- Sample window per case: {SAMPLE_S}s (one sample every {SAMPLE_INTERVAL_S}s, after {WARMUP_S}s warmup)")
    A(f"- Network: yolov8s-coco, target_fps=8")
    A("")

    # --- Pipeline performance ----------------------------------------------
    A("## 1. Pipeline performance")
    A("")
    A("`inference_ms` is the per-frame NPU inference latency reported by Voyager's heartbeat. "
      "`inferenced_per_s` and `tracked_per_s` are global throughput across all streams (from `.logs/throughput.log`). "
      "`dropped_per_s` counts frames the analytics shards rejected.")
    A("")
    A("| Streams | Inference fps (avg / max) | Inference ms (avg / p95 / max) | Tracked /s avg | Dropped /s avg | Dropped total |")
    A("|--------:|--------------------------:|-------------------------------:|---------------:|---------------:|--------------:|")
    for s in summaries:
        hb = s["edge_heartbeat"]
        tp = s["throughput"]
        A(f"| {s['target_streams']} "
          f"| {_fmt(hb['inference_fps']['avg'])} / {_fmt(hb['inference_fps']['max'])} "
          f"| {_fmt(hb['inference_ms']['avg'])} / {_fmt(hb['inference_ms']['p95'])} / {_fmt(hb['inference_ms']['max'])} "
          f"| {tp['tracked_per_s_avg']} "
          f"| {tp['dropped_per_s_avg']} "
          f"| {tp['dropped_per_s_total']} |")
    A("")

    A("**Tracking latency note:** the pipeline does not currently expose a per-frame tracking-latency metric; "
      "tracking runs on the analytics shards alongside detection forwarding. `dropped_per_s` above is the "
      "proxy: zero drops means the shard kept up with the producer, positive drops means tracking + analytics "
      "could not keep pace.")
    A("")

    # --- Hardware utilization ---------------------------------------------
    A("## 2. Hardware utilization")
    A("")
    A("### CPU")
    A("")
    A("| Streams | Overall busy% (avg / max) | Edge process cpu% (avg / max) | Hottest core (avg / peak) |")
    A("|--------:|--------------------------:|------------------------------:|--------------------------:|")
    for s in summaries:
        ov = s["cpu_overall_busy_pct"]
        hb_cpu = s["edge_heartbeat"]["cpu_pct"]
        # hottest core: pick by peak then break ties by avg
        peaks = s["cpu_per_core_peak"]
        avgs = s["cpu_per_core_avg"]
        if peaks:
            core = max(peaks, key=lambda c: (peaks[c], avgs.get(c, 0)))
            core_str = f"core {core}: {avgs.get(core, 0)} / {peaks[core]}"
        else:
            core_str = "—"
        A(f"| {s['target_streams']} | {_fmt(ov['avg'])} / {_fmt(ov['max'])} "
          f"| {_fmt(hb_cpu['avg'])} / {_fmt(hb_cpu['max'])} "
          f"| {core_str} |")
    A("")

    A("**Per-core averages** (sorted by streams, then by busy%, top 8 cores per case):")
    A("")
    for s in summaries:
        avgs = s["cpu_per_core_avg"]
        if not avgs:
            continue
        top = sorted(avgs.items(), key=lambda kv: -kv[1])[:8]
        cells = " · ".join(f"c{c}={v}" for c, v in top)
        A(f"- `{s['target_streams']}` streams → {cells}")
    A("")

    A("### Memory")
    A("")
    A("| Streams | Host RAM used% (avg / max) | Host RAM used MB (avg / max) | Edge process mem% (avg / max) |")
    A("|--------:|---------------------------:|-----------------------------:|------------------------------:|")
    for s in summaries:
        mp = s["mem_used_pct"]; mm = s["mem_used_mb"]; he = s["edge_heartbeat"]["mem_pct"]
        A(f"| {s['target_streams']} | {_fmt(mp['avg'])} / {_fmt(mp['max'])} "
          f"| {_fmt(mm['avg'])} / {_fmt(mm['max'])} "
          f"| {_fmt(he['avg'])} / {_fmt(he['max'])} |")
    A("")

    A("### Video decode (iGPU via VA-API)")
    A("")
    A("`intel_gpu_top` engine averages across the sample window. Video / VideoEnhance engines are the "
      "VA-API decode path used by libav. Render/3D and Blitter are typically near zero on this workload.")
    A("")
    # collect a canonical set of engine names that appear in any case
    engine_set = sorted({k for s in summaries for k in s.get("igpu_engines_avg", {}).keys()})
    if engine_set:
        head = "| Streams | " + " | ".join(engine_set) + " |"
        sep = "|--------:" + ("|--------:" * len(engine_set)) + "|"
        A(head)
        A(sep)
        for s in summaries:
            row = "| " + str(s["target_streams"])
            ig = s.get("igpu_engines_avg", {})
            for e in engine_set:
                row += f" | {_fmt(ig.get(e))}"
            row += " |"
            A(row)
    else:
        A("_iGPU sampling unavailable — `sudo intel_gpu_top` failed for every sample. Check passwordless sudo for the test user._")
    A("")

    A("### NPU (Axelera Metis) — inference")
    A("")
    A("Metis exposes per-frame inference latency through Voyager's stream stats, surfaced in the edge "
      "heartbeat as `inference_ms`. The aggregate frames/sec across all streams is `inference_fps`.")
    A("")
    A("| Streams | Inference ms (avg / max) | Inference fps (avg / max) | Cameras active (avg / max) |")
    A("|--------:|-------------------------:|--------------------------:|---------------------------:|")
    for s in summaries:
        hb = s["edge_heartbeat"]
        A(f"| {s['target_streams']} "
          f"| {_fmt(hb['inference_ms']['avg'])} / {_fmt(hb['inference_ms']['max'])} "
          f"| {_fmt(hb['inference_fps']['avg'])} / {_fmt(hb['inference_fps']['max'])} "
          f"| {_fmt(hb['cameras_active']['avg'])} / {_fmt(hb['cameras_active']['max'])} |")
    A("")

    # --- Data integrity ----------------------------------------------------
    A("## 3. Data integrity")
    A("")
    A("Counts come from grepping `.logs/edge.log` over each case's sampling window for ERROR / CRITICAL / "
      "Traceback / mark_errored / fatal markers. `dropped_per_s_total` is the cumulative analytics-shard drop count.")
    A("")
    A("| Streams | Cameras active (avg) | Cameras errored (avg) | Dropped frames | edge.log error lines | Top error kind |")
    A("|--------:|---------------------:|----------------------:|---------------:|---------------------:|:---------------|")
    for s in summaries:
        hb = s["edge_heartbeat"]
        di = s["data_integrity"]; tp = s["throughput"]
        top_kind = "—"
        if di["by_kind"]:
            top_kind = max(di["by_kind"].items(), key=lambda kv: kv[1])
            top_kind = f"{top_kind[0]} ({top_kind[1]})"
        A(f"| {s['target_streams']} "
          f"| {_fmt(hb['cameras_active']['avg'])} "
          f"| {_fmt(hb['cameras_errored']['avg'])} "
          f"| {tp['dropped_per_s_total']} "
          f"| {di['count']} "
          f"| {top_kind} |")
    A("")

    for s in summaries:
        di = s["data_integrity"]
        if not di["head"]:
            continue
        A(f"### First error lines @ {s['target_streams']} streams")
        A("")
        A("```")
        for line in di["head"]:
            A(line.rstrip())
        A("```")
        A("")

    # --- Raw appendix ------------------------------------------------------
    A("## Appendix — raw per-case summary JSON")
    A("")
    A("```json")
    A(json.dumps(summaries, indent=2, default=str))
    A("```")

    out_path.write_text("\n".join(lines) + "\n")
    print(f"\nReport written: {out_path}")


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


# --------------------------------------------------------------------------
# Top-level orchestration
# --------------------------------------------------------------------------

async def cleanup_bench_cameras(client: httpx.AsyncClient) -> int:
    bench = await list_bench_cameras(client)
    deleted = 0
    for c in bench:
        if await delete_camera(client, c):
            deleted += 1
    return deleted


async def main():
    started_at = dt.datetime.now()
    print(f"=== Benchmark started {started_at.isoformat()} ===", flush=True)

    watcher = HeartbeatWatcher()
    watcher.start()
    # give the broker subscribe + retained heartbeat a moment to land
    await asyncio.sleep(2)

    async with httpx.AsyncClient() as client:
        # Sanity: cloud + edge reachable
        try:
            await client.get(f"{API}/api/cameras", timeout=5.0)
        except Exception as exc:
            print(f"FATAL: cloud API not reachable at {API}: {exc}", flush=True)
            sys.exit(1)
        try:
            await client.get(f"{EDGE_HTTP}/camera_stats", timeout=3.0)
        except Exception as exc:
            print(f"FATAL: edge HTTP not reachable at {EDGE_HTTP}: {exc}", flush=True)
            sys.exit(1)

        summaries: list[dict] = []
        try:
            for case in CASES:
                summary = await run_case(case, client, watcher)
                summaries.append(summary)
                # Persist incremental snapshot so a crash mid-run still gives partial data.
                (BENCH_DIR / f"_inprogress_{started_at.strftime('%Y%m%d_%H%M')}.json").write_text(
                    json.dumps(summaries, indent=2, default=str)
                )
        finally:
            print("\n=== cleanup: deleting bench cameras ===", flush=True)
            deleted = await cleanup_bench_cameras(client)
            print(f"  deleted {deleted} bench cameras", flush=True)
            # Restart edge one more time so it drops the bench cams cleanly
            # (otherwise it keeps trying to stream from them until next boot).
            kick_edge_restart()

    watcher.stop()

    if summaries:
        ts = started_at.strftime("%Y%m%d_%H%M")
        json_path = BENCH_DIR / f"bench_{ts}.json"
        json_path.write_text(json.dumps(summaries, indent=2, default=str))
        md_path = BENCH_DIR / f"bench_report_{ts}.md"
        write_markdown(summaries, md_path, started_at)
        # remove the in-progress file now that the final report is written
        try:
            (BENCH_DIR / f"_inprogress_{ts}.json").unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
