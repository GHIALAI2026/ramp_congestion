from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Database -----------------------------------------------------------
    # Credentials MUST be supplied via the environment (see deploy/.env.example).
    # No default password is shipped: a production deployment must not retain
    # default DB credentials (security observation #5). The connection URLs are
    # assembled from these parts so the password never lives in source.
    db_user: str = "vzi_app"
    db_password: str = ""          # REQUIRED — set VZI_DB_PASSWORD
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "vehicle_zone"

    redis_url: str = "redis://localhost:6379/1"  # DB 1 to avoid collision with ApexEdge
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    edge_id: str = "vehicle-edge-01"
    mqtt_topic_prefix: str = "vehicle"
    edge_http_base_url: str = "http://localhost:8003"
    probe_camera_snapshots: bool = False
    mqtt_ingest_queue_max: int = 1000

    # --- Web security -------------------------------------------------------
    # CORS is restricted to the approved dashboard origin(s) only — no wildcard
    # (security observation #6). Comma-separated. In production set
    # VZI_CORS_ALLOW_ORIGINS to the HTTPS dashboard origin, e.g.
    # "https://vehicle-dash.example.lan". Defaults cover the local console.
    cors_allow_origins: str = "http://localhost:8002,http://127.0.0.1:8002"

    # Connecting peers whose forwarded client-IP headers we trust. This is the
    # Nginx reverse proxy, which runs on the same host as the loopback-bound
    # app (security observation #2). Comma-separated. See cloud/modules/auth.py.
    trusted_proxy_ips: str = "127.0.0.1,::1"

    model_config = {"env_prefix": "VZI_"}

    @model_validator(mode="after")
    def _require_db_password(self) -> "Settings":
        if not self.db_password:
            raise ValueError(
                "Database password is not set. Provide VZI_DB_PASSWORD "
                "(copy deploy/.env.example to deploy/.env and fill it in). "
                "Default database credentials are not permitted."
            )
        return self

    @property
    def database_url_raw(self) -> str:
        """asyncpg / psql style URL (no driver suffix)."""
        return (
            f"postgresql://{self.db_user}:{quote(self.db_password, safe='')}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def database_url(self) -> str:
        """SQLAlchemy-style URL with the asyncpg driver."""
        return (
            f"postgresql+asyncpg://{self.db_user}:{quote(self.db_password, safe='')}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def trusted_proxies(self) -> set[str]:
        return {p.strip() for p in self.trusted_proxy_ips.split(",") if p.strip()}


settings = Settings()
