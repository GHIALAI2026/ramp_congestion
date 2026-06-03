"""Vehicle Zone Intelligence — Edge agent configuration."""
import os
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[3]

# Voyager short model names resolve under AXELERA_FRAMEWORK/build.
# Default it to the local SDK root so manual launches work the same way as
# the queue-management project when selecting built-in SDK networks.
os.environ.setdefault("AXELERA_FRAMEWORK", str(SDK_ROOT))

EDGE_ID = os.environ.get("EDGE_ID", "vehicle-edge-01")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
CLOUD_API_URL = os.environ.get("CLOUD_API_URL", "http://localhost:8002")
HTTP_PORT = int(os.environ.get("EDGE_HTTP_PORT", "8003"))

# Redis is used to persist per-track zone-entry timestamps across edge restarts,
# so overstay alerts reflect the vehicle's actual continuous dwell rather than
# "time since the current edge process first saw it." Without this, every
# restart silently resets the dwell clock and every overstay alert reads
# exactly max_dwell_time_s.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")

# How often to re-fire an overstay alert while a vehicle is still in the zone.
# Each re-fire carries the current (growing) dwell value, so a vehicle parked
# for an hour produces alerts at 15/20/25/.../60 min instead of one frozen
# 15-min row.
OVERSTAY_REALERT_INTERVAL_S = float(os.environ.get("OVERSTAY_REALERT_INTERVAL_S", "300"))

# Multiplier applied to max_dwell_time_s when setting Redis TTL on entry-time
# keys. 4× means a vehicle whose Redis key was last written an hour ago (for
# the default 900s threshold) is considered stale and the new detection gets
# a fresh entry timestamp. Tuneable for sites with very long parking.
OVERSTAY_ENTRY_TTL_MULTIPLIER = float(os.environ.get("OVERSTAY_ENTRY_TTL_MULTIPLIER", "4"))

# Voyager SDK — Axelera Metis AIPU network name
# "yolov8s-coco" = built-in COCO model (80 classes)
VOYAGER_NETWORK = os.environ.get("VOYAGER_NETWORK", "yolov8s-coco")
VOYAGER_AIPU_CORES = int(
    os.environ.get(
        "VOYAGER_AIPU_CORES",
        "4" if VOYAGER_NETWORK.startswith("vehicle-detection-") else "1",
    )
)
VOYAGER_MAX_SOURCES_PER_PIPELINE = int(
    os.environ.get(
        "VOYAGER_MAX_SOURCES_PER_PIPELINE",
        "32" if VOYAGER_NETWORK.startswith("vehicle-detection-") else "10",
    )
)
VOYAGER_ALLOW_HARDWARE_CODEC = (
    os.environ.get("VOYAGER_ALLOW_HARDWARE_CODEC", "1") != "0"
)
VOYAGER_ENABLE_VAAPI = os.environ.get("VOYAGER_ENABLE_VAAPI", "1") != "0"


def _parse_vehicle_class_map(raw: str | None) -> dict[int, str] | None:
    if not raw:
        return None
    mapping: dict[int, str] = {}
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition(":")
        if not sep:
            continue
        mapping[int(key.strip())] = value.strip()
    return mapping or None


def _default_vehicle_class_names(network: str) -> dict[int, str]:
    # Custom compiled network from Vehicle_Detection_v8n.pt
    if network.startswith("vehicle-detection-"):
        return {0: "car", 1: "bus", 2: "truck"}

    # Built-in COCO vehicle classes
    return {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


# Optional override example: "0:car,1:bus,2:truck"
VEHICLE_CLASS_NAMES = (
    _parse_vehicle_class_map(os.environ.get("VEHICLE_CLASS_MAP"))
    or _default_vehicle_class_names(VOYAGER_NETWORK)
)
VEHICLE_CLASS_IDS = sorted(VEHICLE_CLASS_NAMES)
DEFAULT_VEHICLE_CLASS_ID = VEHICLE_CLASS_IDS[0] if VEHICLE_CLASS_IDS else 0

# Pipeline target FPS
TARGET_FPS = float(os.environ.get("TARGET_FPS", "8"))
CALLBACK_QUEUE_SIZE = int(os.environ.get("CALLBACK_QUEUE_SIZE", "1"))
CALLBACK_DROP_LOG_INTERVAL_S = float(os.environ.get("CALLBACK_DROP_LOG_INTERVAL_S", "10.0"))
SNAPSHOT_CACHE_FPS = float(os.environ.get("SNAPSHOT_CACHE_FPS", "2"))
LIVE_ANNOTATE_FPS = float(os.environ.get("LIVE_ANNOTATE_FPS", "2"))
LIVE_FRAME_STALE_S = float(os.environ.get("LIVE_FRAME_STALE_S", "1.5"))
# When no viewer is registered for a camera, the iter thread skips the
# 6.22 MB SHM memcpy. After the last viewer disconnects we keep writing
# for this grace window so quick reconnects (page reload, brief tab
# switch) don't fall through to the lazy OpenCV decoder.
FRAME_VIEWER_GRACE_S = float(os.environ.get("FRAME_VIEWER_GRACE_S", "30.0"))
INFERENCE_HEALTH_STALE_S = float(os.environ.get("INFERENCE_HEALTH_STALE_S", "5.0"))
INFERENCE_STARTUP_GRACE_S = float(os.environ.get("INFERENCE_STARTUP_GRACE_S", "20.0"))
ENABLE_SOURCE_PREFLIGHT = os.environ.get("ENABLE_SOURCE_PREFLIGHT", "1") != "0"
STARTUP_SOURCE_PREFLIGHT_S = float(os.environ.get("STARTUP_SOURCE_PREFLIGHT_S", "5.0"))
LOCAL_SOURCE_PREFLIGHT_S = float(os.environ.get("LOCAL_SOURCE_PREFLIGHT_S", "10.0"))
LOCAL_SOURCE_RETRY_WINDOW_S = float(os.environ.get("LOCAL_SOURCE_RETRY_WINDOW_S", "90.0"))
# Codec-level preflight (ffprobe). Catches cameras that pass TCP preflight
# but stream malformed video (broken HEVC PPS, dead encoder, etc.). Such
# sources would otherwise wedge the Voyager SDK call for 30-60 s, holding
# the engine deploy lock and blocking every other camera's startup.
RTSP_CODEC_PREFLIGHT_TIMEOUT_S = float(
    os.environ.get("RTSP_CODEC_PREFLIGHT_TIMEOUT_S", "5.0")
)
# Cap for the exponential-backoff retry interval used when a source
# fails preflight. The retry loop runs indefinitely while the camera is
# registered, doubling the wait from 1 s up to this cap.
SOURCE_RETRY_MAX_INTERVAL_S = float(
    os.environ.get("SOURCE_RETRY_MAX_INTERVAL_S", "30.0")
)
# Startup source-add throttle. Adding many Voyager sources concurrently
# overwhelms the SDK and leaves a random subset of cameras attached-but-dark
# (they ping fine but never deliver inference frames). Serialize the startup
# adds (one at a time) and pause this long between each so the SDK settles
# each source before the next begins.
SOURCE_ADD_THROTTLE_S = float(
    os.environ.get("SOURCE_ADD_THROTTLE_S", "1.5")
)
STARTUP_ADD_CONCURRENCY = int(
    os.environ.get("STARTUP_ADD_CONCURRENCY", "1")
)
KEEP_FALLBACK_DECODER_OPEN = os.environ.get("KEEP_FALLBACK_DECODER_OPEN", "0") != "0"
HTTP_ACCESS_LOG = os.environ.get("HTTP_ACCESS_LOG", "0") != "0"

# MQTT telemetry is high-volume and disposable; alerts stay reliable by default.
MQTT_TELEMETRY_QOS = int(os.environ.get("MQTT_TELEMETRY_QOS", "0"))
MQTT_ALERT_QOS = int(os.environ.get("MQTT_ALERT_QOS", "1"))
MQTT_MAX_INFLIGHT = int(os.environ.get("MQTT_MAX_INFLIGHT", "20"))
MQTT_MAX_QUEUED = int(os.environ.get("MQTT_MAX_QUEUED", "200"))

# Edge telemetry collection
SYSTEM_METRICS_CACHE_S = float(os.environ.get("SYSTEM_METRICS_CACHE_S", "4.0"))
INTEL_GPU_TOP_BIN = os.environ.get("INTEL_GPU_TOP_BIN", "intel_gpu_top")
INTEL_GPU_TOP_SAMPLE_MS = int(os.environ.get("INTEL_GPU_TOP_SAMPLE_MS", "500"))
INTEL_GPU_TOP_TIMEOUT_S = float(os.environ.get("INTEL_GPU_TOP_TIMEOUT_S", "2.0"))
AXSYSTEMSERVER_BIN = os.environ.get(
    "AXSYSTEMSERVER_BIN",
    "/opt/axelera/runtime-1.5.4-rc1-1/bin/axsystemserver",
)
AXSYSTEMSERVER_STARTUP_S = float(os.environ.get("AXSYSTEMSERVER_STARTUP_S", "1.0"))
AXMONITOR_BIN = os.environ.get(
    "AXMONITOR_BIN",
    "/opt/axelera/runtime-1.5.4-rc1-1/bin/axmonitor",
)
AXMONITOR_TIMEOUT_S = float(os.environ.get("AXMONITOR_TIMEOUT_S", "8.0"))

# Tracker tuning (OC-SORT — tuned for vehicles: larger bboxes, slower movement)
OCSORT_HIGH_THRESH = float(os.environ.get("OC_HIGH_THRESH", "0.4"))
OCSORT_LOW_THRESH = float(os.environ.get("OC_LOW_THRESH", "0.1"))
OCSORT_MATCH_THRESH = float(os.environ.get("OC_MATCH_THRESH", "0.5"))
OCSORT_MAX_TIME_LOST = int(os.environ.get(
    "OC_MAX_TIME_LOST", str(max(30, int(TARGET_FPS * 15)))))  # 15s for vehicles
OCSORT_BBOX_INFLATION = float(os.environ.get("OC_BBOX_INFLATION", "1.5"))

# Inference
INFERENCE_CONF = float(os.environ.get("INF_CONF", "0.18"))
INFERENCE_IOU = float(os.environ.get("INF_IOU", "0.3"))
MIN_VEHICLE_BBOX_AREA = float(os.environ.get("MIN_VEHICLE_BBOX_AREA", "400"))
MAX_VEHICLE_BBOX_AREA = float(os.environ.get("MAX_VEHICLE_BBOX_AREA", "800000"))
MIN_VEHICLE_ASPECT = float(os.environ.get("MIN_VEHICLE_ASPECT", "0.18"))
MAX_VEHICLE_ASPECT = float(os.environ.get("MAX_VEHICLE_ASPECT", "8.0"))

# Dashboard Konva canvas size — polygon coordinates are stored in this space
CANVAS_W = int(os.environ.get("CANVAS_W", "800"))
CANVAS_H = int(os.environ.get("CANVAS_H", "500"))

# MQTT topic prefix
MQTT_TOPIC_PREFIX = "vehicle"

# ------------------------------------------------------------------
# Multi-process pipeline tuning
# ------------------------------------------------------------------

# Max expected frame resolution. SharedMemory slots are sized for this;
# larger frames are dropped with a warning. Default covers 1080p.
MAX_FRAME_W = int(os.environ.get("MAX_FRAME_W", "1920"))
MAX_FRAME_H = int(os.environ.get("MAX_FRAME_H", "1080"))

# Max JPEG payload per camera. 700 KB fits a heavily annotated 720p frame
# at quality 70 with comfortable headroom.
MAX_JPEG_BYTES = int(os.environ.get("MAX_JPEG_BYTES", str(700 * 1024)))

# Live preview is rescaled to this max width before encode.
LIVE_PREVIEW_MAX_W = int(os.environ.get("LIVE_PREVIEW_MAX_W", "1280"))
LIVE_JPEG_QUALITY = int(os.environ.get("LIVE_JPEG_QUALITY", "70"))

# Number of analytics shards. Defaults to half of CPU cores, capped to 8.
try:
    _default_shards = max(1, min(8, (os.cpu_count() or 4) // 2))
except Exception:
    _default_shards = 2
NUM_ANALYTICS_SHARDS = int(os.environ.get("NUM_ANALYTICS_SHARDS", str(_default_shards)))

# Number of UI render workers. One viewer at a time means 1–2 is enough.
NUM_UI_WORKERS = int(os.environ.get("NUM_UI_WORKERS", "1"))

# When True, attempt to use TurboJPEG for encode; falls back to OpenCV.
USE_TURBOJPEG = os.environ.get("USE_TURBOJPEG", "1") != "0"

# TCP-only preflight uses this port-probe timeout instead of opening RTSP.
PREFLIGHT_TCP_TIMEOUT_S = float(os.environ.get("PREFLIGHT_TCP_TIMEOUT_S", "1.5"))

# Max camera slots — sized once, only matters as the upper bound for the
# lock-free viewer counter Array.
MAX_CAMERA_SLOTS = int(os.environ.get("MAX_CAMERA_SLOTS", "128"))

# Detection ring — fixed per-camera slot in shared memory carrying the
# latest detection list (replaces bulk pickle on the analytics queue).
# 64 dets per frame is generous; YOLOv8s on a vehicle scene typically
# returns 5–25 after the geometric filters.
DET_RING_MAX_DETS = int(os.environ.get("DET_RING_MAX_DETS", "64"))

# Per-camera ring depth: number of sub-slots each camera has in the
# detection ring. The consumer can fall up to this many frames behind
# without losing data. At 8 fps target, 16 = 2 seconds of buffer.
DET_RING_DEPTH = int(os.environ.get("DET_RING_DEPTH", "16"))

# Per-second pipeline throughput log. Truncated at agent start.
THROUGHPUT_LOG_PATH = os.environ.get(
    "EDGE_THROUGHPUT_LOG",
    str(Path(__file__).resolve().parents[1] / ".logs" / "throughput.log"),
)
THROUGHPUT_LOG_INTERVAL_S = float(
    os.environ.get("EDGE_THROUGHPUT_LOG_INTERVAL_S", "1.0"),
)
