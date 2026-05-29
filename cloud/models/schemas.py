"""Cloud-side Pydantic schemas for vehicle zone intelligence."""

from __future__ import annotations
from pydantic import BaseModel


class VehicleZoneMetricsMsg(BaseModel):
    """Must match edge_agent.schemas.VehicleZoneMetrics exactly."""
    v: int = 1
    edge_id: str
    zone_id: str
    camera_id: str
    ts: float
    vehicle_count: int
    vehicle_count_by_type: dict[str, int]
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


class VehicleAlertMsg(BaseModel):
    """Must match edge_agent.schemas.VehicleAlert exactly."""
    v: int = 1
    edge_id: str
    zone_id: str
    camera_id: str
    ts: float
    alert_type: str
    level: str
    message: str
    vehicle_count: int | None = None
    dwell_time_s: float | None = None
    track_id: int | None = None
    bbox: list[float] | None = None
    # Source frame dims that `bbox` is expressed in. None on legacy alerts.
    frame_w: int | None = None
    frame_h: int | None = None
    # Zone polygon (list of [x, y]) in the same frame coords as `bbox`.
    zone_poly: list[list[float]] | None = None


class EdgeHeartbeatMsg(BaseModel):
    """Must match edge_agent.schemas.EdgeHeartbeat exactly."""
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
