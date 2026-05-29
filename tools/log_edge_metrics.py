#!/usr/bin/env python3
"""Poll /api/overview every minute for 24 hours and append per-edge
telemetry to .logs/edge_metrics.jsonl as one JSON object per line.

Run with `nohup` so it survives terminal closure:

    nohup python3 tools/log_edge_metrics.py > .logs/edge_metrics.runner.log 2>&1 &

Stop early with `kill <pid>`. The script self-terminates after 1440
iterations (24h × 60 min).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_URL = "http://localhost:8002/api/overview"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / ".logs" / "edge_metrics.jsonl"


def fetch(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        sys.stderr.write(f"[{datetime.now().isoformat()}] fetch failed: {exc}\n")
        sys.stderr.flush()
        return None


def write_line(out: Path, record: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
        f.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between polls (default 60)")
    ap.add_argument("--duration", type=float, default=24 * 60 * 60,
                    help="total runtime in seconds (default 86400 = 24h)")
    args = ap.parse_args()

    start = time.monotonic()
    iteration = 0
    sys.stderr.write(
        f"[{datetime.now().isoformat()}] starting — "
        f"polling {args.url} every {args.interval}s for {args.duration}s, "
        f"writing to {args.out}\n"
    )
    sys.stderr.flush()

    next_fire = start
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= args.duration:
            break

        payload = fetch(args.url)
        poll_ts = datetime.now(timezone.utc).isoformat()
        if payload is not None:
            edges = payload.get("edges") or []
            for edge in edges:
                # Just the 5 utilization bars from the Edge Status page,
                # plus poll_ts so the file is a useful time-series.
                record = {
                    "poll_ts": poll_ts,
                    "cpu_pct": edge.get("cpu_pct"),
                    "mem_pct": edge.get("mem_pct"),
                    "igpu_pct": edge.get("igpu_pct"),
                    "igpu_video_enhance_pct": edge.get("igpu_video_enhance_pct"),
                    "gpu_pct": edge.get("gpu_pct"),
                }
                write_line(args.out, record)
            if not edges:
                # Still log the gap so the timeline shows the outage.
                write_line(args.out, {
                    "poll_ts": poll_ts,
                    "iteration": iteration,
                    "edge_id": None,
                    "note": "no edges in /api/overview response",
                })
        else:
            write_line(args.out, {
                "poll_ts": poll_ts,
                "iteration": iteration,
                "edge_id": None,
                "note": "fetch failed",
            })

        iteration += 1
        # Drift-resistant scheduling: aim for absolute slot boundaries.
        next_fire += args.interval
        sleep_for = next_fire - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            # Fell behind. Reset to "now" so we don't burst.
            next_fire = time.monotonic()

    sys.stderr.write(
        f"[{datetime.now().isoformat()}] done — wrote {iteration} iterations\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
