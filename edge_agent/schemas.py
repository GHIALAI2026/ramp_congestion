"""Vehicle Zone Intelligence — Edge agent Pydantic schemas.

VehicleZoneMetrics must match cloud/models/schemas.py VehicleZoneMetricsMsg exactly.
"""

from __future__ import annotations
from pydantic import BaseModel


class ZoneConfig(BaseModel):
    zone_id: str
    camera_id: str
    name: str
    zone_poly: list[list[float]] | None = None   # [[x, y], ...] in canvas coords
    max_vehicles: int = 20                        # overcrowding threshold
    max_dwell_time_s: float = 900.0               # overstay threshold (seconds)


class CameraConfig(BaseModel):
    camera_id: str
    name: str | None = None
    source_url: str
    assigned_edge: str
    zones: list[ZoneConfig] = []


class VehicleZoneMetrics(BaseModel):
    """Must match VehicleZoneMetricsMsg in cloud/models/schemas.py exactly."""
    v: int = 1
    edge_id: str
    zone_id: str
    camera_id: str
    ts: float
    vehicle_count: int
    vehicle_count_by_type: dict[str, int]   # {"car": 5, "truck": 2, ...}
    max_vehicles: int
    occupancy_pct: float
    overstay_count: int
    avg_dwell_time_s: float
    max_dwell_time_s: float
    total_entered: int
    total_exited: int
    active_track_count: int
    overcrowding_alert: bool
    overstay_alert_ids: list[int]
    inf_fps: float
    inf_ms: float


class VehicleAlert(BaseModel):
    """Alert message sent via MQTT."""
    v: int = 1
    edge_id: str
    zone_id: str
    camera_id: str
    ts: float
    alert_type: str       # "overcrowding" or "overstay"
    level: str            # "warning" or "critical"
    message: str
    vehicle_count: int | None = None
    dwell_time_s: float | None = None
    track_id: int | None = None
    # bbox in (x1, y1, x2, y2) frame-pixel coords, expressed in the source
    # frame's coordinate space (frame_w × frame_h below). For cameras whose
    # native resolution exceeds the edge's shm preview slot the snapshot
    # served to the cloud is downscaled, so the cloud must rescale this bbox
    # by (snapshot.w / frame_w, snapshot.h / frame_h) before crop/draw.
    bbox: list[float] | None = None
    # Source frame dimensions used for inference (and therefore for `bbox`).
    # None means "unknown / legacy" — cloud falls back to assuming the bbox
    # is already in snapshot pixel space.
    frame_w: int | None = None
    frame_h: int | None = None
    # Zone polygon as a list of [x, y] points in the SAME coordinate space
    # as `bbox` (the source frame_w × frame_h). Allows the cloud to draw
    # the offending zone's outline onto the evidence snapshot so an
    # operator can see — on a multi-zone camera — which side of the speed
    # breaker (or other physical divider) the alert came from. None for
    # alert types that aren't zone-scoped.
    zone_poly: list[list[float]] | None = None


class EdgeHeartbeat(BaseModel):
    """Must match EdgeHeartbeatMsg in cloud/models/schemas.py exactly."""
    v: int = 1
    edge_id: str
    ts: float
    uptime_s: float
    cameras_active: int
    cameras_assigned: int
    cameras_errored: int
    zones_active: int
    cpu_pct: float
    mem_pct: float
    gpu_pct: float | None = None
    gpu_mem_pct: float | None = None
    gpu_name: str | None = None
    gpu_note: str | None = None
    gpu_temp_c: float | None = None
    gpu_mem_used_mb: float | None = None
    gpu_mem_total_mb: float | None = None
    gpu_active_cores: int | None = None
    gpu_total_cores: int | None = None
    igpu_name: str | None = None
    igpu_pct: float | None = None
    igpu_video_pct: float | None = None
    igpu_video_enhance_pct: float | None = None
    igpu_note: str | None = None
    inference_fps: float | None = None
    inference_ms: float | None = None
    error_cameras: list[str] = []
