#!/usr/bin/env python3
"""Probe every RTSP camera URL and emit a stream-profile markdown table.

Used to enrich the benchmark report. Runs ffprobe in parallel and gathers
codec / resolution / advertised fps / bitrate per source.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import asyncpg
import asyncio

FFPROBE = shutil.which("ffprobe") or "/opt/ffmpeg/ffprobe"
TIMEOUT_S = 10.0

BENCH_URLS = [
    ("bench_cam_01", "rtsp://kspoc:kspoc123@10.64.0.11/stream1"),
    ("bench_cam_02", "rtsp://admin:Ap0%28%402024@10.86.95.22/videoStreamId=2"),
    ("bench_cam_03", "rtsp://kspoc:kspoc123@10.64.0.13/stream1"),
    ("bench_cam_04", "rtsp://kspoc:kspoc123@10.64.0.14/stream1"),
    ("bench_cam_05", "rtsp://kspoc:kspoc123@10.64.0.15/stream1"),
    ("bench_cam_06", "rtsp://kspoc:kspoc123@10.64.0.16/stream1"),
    ("bench_cam_07", "rtsp://kspoc:kspoc123@10.64.0.30/stream1"),
    ("bench_cam_08", "rtsp://kspoc:kspoc123@10.64.0.31/stream1"),
    ("bench_cam_09", "rtsp://kspoc:kspoc123@10.64.0.33/stream1"),
    ("bench_cam_10", "rtsp://kspoc:kspoc123@10.64.0.34/stream1"),
    ("bench_cam_11", "rtsp://kspoc:kspoc123@10.64.0.36/stream1"),
    ("bench_cam_12", "rtsp://kspoc:kspoc123@10.64.0.37/stream1"),
    ("bench_cam_13", "rtsp://kspoc:kspoc123@10.64.0.38/stream1"),
    ("bench_cam_14", "rtsp://kspoc:kspoc123@10.64.0.40/stream1"),
    ("bench_cam_15", "rtsp://kspoc:kspoc123@10.64.0.41/stream1"),
    ("bench_cam_16", "rtsp://kspoc:kspoc123@10.64.0.42/stream1"),
    ("bench_cam_17", "rtsp://kspoc:kspoc123@10.64.0.44/stream1"),
    ("bench_cam_18", "rtsp://kspoc:kspoc123@10.64.0.46/stream1"),
    ("bench_cam_19", "rtsp://kspoc:kspoc123@10.86.158.163/rtsp/defaultPrimary?streamType=u"),
    ("bench_cam_20", "rtsp://kspoc:kspoc123@10.86.158.196/rtsp/defaultPrimary?streamType=u"),
    ("bench_cam_21", "rtsp://kspoc:kspoc123@10.86.158.168/rtsp/defaultPrimary?streamType=u"),
    ("bench_cam_22", "rtsp://kspoc:kspoc123@10.86.158.140/rtsp/defaultPrimary?streamType=u"),
    ("bench_cam_23", "rtsp://kspoc:kspoc123@10.86.158.172/rtsp/defaultPrimary?streamType=u"),
    ("bench_cam_24", "rtsp://kspoc:kspoc123@10.86.158.71:554/videoStreamId=2"),
    ("bench_cam_25", "rtsp://kspoc:kspoc123@10.86.158.179/rtsp/defaultPrimary?streamType=u"),
]


def _parse_rate(v: str) -> float | None:
    v = (v or "").strip()
    if not v or v == "0/0":
        return None
    if "/" in v:
        n, _, d = v.partition("/")
        try:
            nn, dd = float(n), float(d)
            return nn / dd if dd else None
        except ValueError:
            return None
    try:
        return float(v)
    except ValueError:
        return None


def probe(cam_id: str, url: str) -> dict:
    cmd = [
        FFPROBE, "-v", "error", "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,profile,level,width,height,avg_frame_rate,r_frame_rate,bit_rate,pix_fmt",
        "-of", "json",
        url,
    ]
    out = {"cam_id": cam_id, "url": url}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
        if r.returncode != 0:
            out["error"] = (r.stderr or "ffprobe failed").splitlines()[0][:200]
            return out
        info = json.loads(r.stdout).get("streams", [{}])[0]
        out["codec"] = info.get("codec_name")
        out["profile"] = info.get("profile")
        out["width"] = info.get("width")
        out["height"] = info.get("height")
        out["pix_fmt"] = info.get("pix_fmt")
        out["avg_fps"] = _parse_rate(info.get("avg_frame_rate"))
        out["r_fps"] = _parse_rate(info.get("r_frame_rate"))
        out["bit_rate_kbps"] = (
            int(info["bit_rate"]) // 1000 if info.get("bit_rate") else None
        )
    except subprocess.TimeoutExpired:
        out["error"] = f"timeout after {TIMEOUT_S}s"
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out


async def _fetch_prod_cams() -> list[tuple[str, str]]:
    conn = await asyncpg.connect(
        host="localhost", database="vehicle_zone",
        user="apexedge", password="apexedge",
    )
    rows = await conn.fetch("SELECT camera_id, source_url FROM cameras ORDER BY camera_id")
    await conn.close()
    return [(r["camera_id"], r["source_url"]) for r in rows]


def main() -> None:
    prod = asyncio.run(_fetch_prod_cams())

    all_cams = [(cid, url) for cid, url in prod] + list(BENCH_URLS)
    print(f"Probing {len(all_cams)} streams...", file=sys.stderr)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(probe, cid, url) for cid, url in all_cams]
        for f in as_completed(futures):
            results.append(f.result())

    # Preserve input order
    order = {cid: i for i, (cid, _) in enumerate(all_cams)}
    results.sort(key=lambda r: order.get(r["cam_id"], 999))

    # Console output
    ok = sum(1 for r in results if "error" not in r)
    print(f"  reachable: {ok}/{len(results)}", file=sys.stderr)

    # JSON for downstream tooling
    out_json = "/home/admin1/Vehicle_Detection_Git/Vehicle_Detection/bench_results/stream_profile.json"
    with open(out_json, "w") as f:
        json.dump(results, f, indent=2)
    print(out_json)


if __name__ == "__main__":
    main()
