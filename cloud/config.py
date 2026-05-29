from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://apexedge:apexedge@localhost:5432/vehicle_zone"
    database_url_raw: str = "postgresql://apexedge:apexedge@localhost:5432/vehicle_zone"
    redis_url: str = "redis://localhost:6379/1"  # DB 1 to avoid collision with ApexEdge
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    edge_id: str = "vehicle-edge-01"
    mqtt_topic_prefix: str = "vehicle"
    edge_http_base_url: str = "http://localhost:8003"
    probe_camera_snapshots: bool = False
    mqtt_ingest_queue_max: int = 1000

    model_config = {"env_prefix": "VZI_"}


settings = Settings()
