"""
Vehicle Zone Intelligence — Edge agent entry point.

Run with: PYTHONPATH=. python -m edge_agent.main

Startup sequence:
  1. Build CameraManager (owns SharedMemory registries + worker pools)
  2. Build VoyagerEngine wired to the manager's frame registry
  3. Connect MQTT publisher (heartbeats only — analytics shards have their own)
  4. Fetch camera/zone configs from cloud API
  5. Register all cameras (allocates SHM slots up front)
  6. Start analytics + UI worker pools (after SHM dicts are populated)
  7. Actually start each CameraStream (Voyager add_source + preflight)
  8. Start MQTT listener, HTTP snapshot server, heartbeat loop
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import signal
import time

import psutil
import resource



from edge_agent import config as cfg
from edge_agent.comms.mqtt_listener import MQTTListener
from edge_agent.comms.mqtt_publisher import MQTTPublisher
from edge_agent.http_server import run_http_server
from edge_agent.manager.camera_manager import CameraManager
from edge_agent.manager.config_store import ConfigStore
from edge_agent.pipeline.metrics_logger import ThroughputLogger
from edge_agent.pipeline.shm_buffer import (
    FrameBufferRegistry,
    JPEGBufferRegistry,
)
from edge_agent.schemas import EdgeHeartbeat
from edge_agent.system_metrics import SystemMetricsCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vehicle-edge")

def _set_ulimit():
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 65536
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, max(hard, target)))
            logger.info("Increased NOFILE limit from %s to %s", soft, target)
        else:
            logger.info("NOFILE soft limit already adequate: %s", soft)
    except Exception as e:
        logger.warning("Failed to increase NOFILE limit: %s", e)

_set_ulimit()

def _camera_startup_priority(camera) -> tuple[int, int, str]:
    source = (camera.source_url or "").lower()
    is_local = "127.0.0.1" in source or "localhost" in source
    is_stream_path = "/stream" in source
    is_videostream = "videostreamid=" in source
    local_rank = 0 if is_local else 1
    fragility_rank = 0 if (is_stream_path and not is_videostream) else 1
    return (local_rank, fragility_rank, camera.camera_id)


def _create_engine(frame_registry):
    """Create VoyagerEngine for Axelera Metis, wired to the frame registry."""
    from edge_agent.pipeline.voyager_engine import VoyagerEngine
    engine = VoyagerEngine(
        network=cfg.VOYAGER_NETWORK,
        conf=cfg.INFERENCE_CONF,
        iou=cfg.INFERENCE_IOU,
        frame_registry=frame_registry,
    )
    logger.info(
        "Using VoyagerEngine (Axelera Metis, network=%s)", cfg.VOYAGER_NETWORK,
    )
    return engine


async def heartbeat_loop(
    mqtt_pub: MQTTPublisher,
    camera_manager: CameraManager,
    engine,
    start_time: float,
    metrics_collector: SystemMetricsCollector,
):
    while True:
        await asyncio.sleep(5)
        stats = camera_manager.get_stats()
        try:
            telemetry = await asyncio.to_thread(metrics_collector.collect)
        except Exception:
            logger.exception("System metrics collection failed")
            telemetry = {
                "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
                "mem_pct": round(psutil.virtual_memory().percent, 1),
            }
        hb = EdgeHeartbeat(
            edge_id=cfg.EDGE_ID,
            ts=time.time(),
            uptime_s=round(time.time() - start_time),
            cameras_active=stats["cameras_active"],
            cameras_assigned=stats["cameras_assigned"],
            cameras_errored=stats["cameras_errored"],
            zones_active=stats["zones_active"],
            cpu_pct=telemetry.get("cpu_pct", 0.0),
            mem_pct=telemetry.get("mem_pct", 0.0),
            gpu_pct=telemetry.get("gpu_pct"),
            gpu_mem_pct=telemetry.get("gpu_mem_pct"),
            gpu_name=telemetry.get("gpu_name"),
            gpu_note=telemetry.get("gpu_note"),
            gpu_temp_c=telemetry.get("gpu_temp_c"),
            gpu_mem_used_mb=telemetry.get("gpu_mem_used_mb"),
            gpu_mem_total_mb=telemetry.get("gpu_mem_total_mb"),
            gpu_active_cores=telemetry.get("gpu_active_cores"),
            gpu_total_cores=telemetry.get("gpu_total_cores"),
            igpu_name=telemetry.get("igpu_name"),
            igpu_pct=telemetry.get("igpu_pct"),
            igpu_video_pct=telemetry.get("igpu_video_pct"),
            igpu_video_enhance_pct=telemetry.get("igpu_video_enhance_pct"),
            igpu_note=telemetry.get("igpu_note"),
            inference_fps=round(engine.avg_inf_fps, 1) if engine.avg_inf_fps > 0 else None,
            inference_ms=round(engine.avg_inf_ms, 1) if engine.avg_inf_ms > 0 else None,
            error_cameras=stats["error_cameras"],
        )
        mqtt_pub.publish_heartbeat(hb)


async def main():
    start_time = time.time()
    logger.info(
        "=== Vehicle Zone Intelligence Edge Agent starting (edge_id=%s) ===",
        cfg.EDGE_ID,
    )

    # 1. SharedMemory registries (need to exist before engine + manager).
    frame_registry = FrameBufferRegistry(
        max_h=cfg.MAX_FRAME_H, max_w=cfg.MAX_FRAME_W,
    )
    jpeg_registry = JPEGBufferRegistry(max_size=cfg.MAX_JPEG_BYTES)

    engine = _create_engine(frame_registry)
    camera_manager = CameraManager(
        engine=engine,
        frame_registry=frame_registry,
        jpeg_registry=jpeg_registry,
    )

    # 2. Connect MQTT publisher (heartbeats only at this layer)
    mqtt_pub = MQTTPublisher()
    mqtt_pub.connect()

    # 2a. MQTT listener for runtime camera/zone updates.
    #     Created EARLY (before camera SHM allocation and worker spawning)
    #     so its socket lands on a low file descriptor. paho-mqtt 2.x uses
    #     select.select() inside its loop, which is hard-capped at
    #     FD_SETSIZE=1024 on Linux/CPython. With 26 cameras the edge racks
    #     up >2000 open FDs and the listener's socket ends up unreadable,
    #     so on_connect never fires and the subscribe never lands.
    listener = MQTTListener(camera_manager)
    listener.start()

    # 3. Fetch configs from cloud
    store = ConfigStore()
    cameras = store.load()
    logger.info("Cameras to start: %d", len(cameras))
    for c in cameras:
        logger.info("  %s (%s) — %d zones", c.camera_id, c.source_url, len(c.zones))

    cameras.sort(key=_camera_startup_priority)
    logger.info(
        "Voyager startup order: %s",
        [f"{c.camera_id}<{c.source_url}>" for c in cameras],
    )

    # 4. Pre-register every camera so SHM slots exist before the worker
    # processes are spawned. We use add_camera() to allocate the slot,
    # but we DEFER calling stream.start() until workers are alive — so
    # do the slot/SHM/stream registration here, then start, then start workers.
    # CameraManager.add_camera already calls stream.start, but at this
    # point analytics_pool/ui_pool are not yet started — that is fine
    # for slot allocation. The streams will simply queue their first
    # CONFIG/DATA messages; analytics shards drain them as soon as they
    # boot.
    # Throttle startup source additions. Firing many add_camera calls at
    # once (was Semaphore(10)) hammered the Voyager SDK with concurrent
    # add_source operations and left a random ~5 of 23 cameras
    # attached-but-dark on every restart. Serialize (concurrency=1) and
    # space them out so each source fully attaches before the next begins.
    startup_semaphore = asyncio.Semaphore(max(1, cfg.STARTUP_ADD_CONCURRENCY))

    async def _safe_start(cam):
        async with startup_semaphore:
            await asyncio.to_thread(
                camera_manager.add_camera, cam.camera_id, cam.source_url, cam.zones,
            )
            if cfg.SOURCE_ADD_THROTTLE_S > 0:
                await asyncio.sleep(cfg.SOURCE_ADD_THROTTLE_S)

    await asyncio.gather(*(_safe_start(cam) for cam in cameras))

    # 5. Now that every initial camera has a SHM slot, spawn the workers.
    # On Linux's default fork start_method, the child inherits the SHM
    # dicts that the manager built up. (For spawn we'd need to pass the
    # dicts explicitly; the renderer pool does that already.)
    camera_manager.start_workers()

    metrics_collector = SystemMetricsCollector()

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("Shutting down edge agent...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    # 7. Per-second throughput logger
    throughput_logger = ThroughputLogger(
        log_path=cfg.THROUGHPUT_LOG_PATH,
        snapshot_fn=lambda: {
            "inferenced": engine.total_frames_inferenced,
            "tracked": camera_manager.total_frames_tracked(),
            "cams_active": camera_manager.get_stats()["cameras_active"],
        },
        interval_s=cfg.THROUGHPUT_LOG_INTERVAL_S,
    )
    throughput_logger.start()

    # 8. HTTP server + heartbeat
    http_task = asyncio.create_task(run_http_server(camera_manager))
    hb_task = asyncio.create_task(
        heartbeat_loop(mqtt_pub, camera_manager, engine, start_time, metrics_collector),
    )

    logger.info("Edge agent running. Cameras streaming to zones.")

    await stop_event.wait()

    # Cleanup
    http_task.cancel()
    hb_task.cancel()
    throughput_logger.stop()
    listener.stop()
    camera_manager.stop_all()
    if hasattr(engine, "stop_all"):
        engine.stop_all()
    metrics_collector.close()
    mqtt_pub.disconnect()
    logger.info("Edge agent stopped.")


if __name__ == "__main__":
    # Linux fork is required for SHM/Array inheritance in our worker pools.
    try:
        multiprocessing.set_start_method("fork", force=False)
    except RuntimeError:
        pass
    asyncio.run(main())
