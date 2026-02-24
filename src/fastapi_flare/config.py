from __future__ import annotations

from typing import Any, Optional

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FlareConfig(BaseSettings):
    """
    Configuration for fastapi-flare.

    All fields can be set via environment variables with the FLARE_ prefix,
    or loaded from a .env file automatically.

    Redis can be configured two ways:

    Option A — individual fields (recommended, avoids URL-encoding issues):
        FLARE_REDIS_HOST=myhost
        FLARE_REDIS_PORT=6379
        FLARE_REDIS_PASSWORD=my&special#password
        FLARE_REDIS_DB=1

    Option B — full URL (takes precedence when set):
        FLARE_REDIS_URL=redis://:password@myhost:6379/1
    """

    # ── Redis: individual fields (Option A — recommended) ────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: Optional[str] = None
    redis_db: int = 0

    # ── Redis: full URL override (Option B) ──────────────────────────────────
    # When set, takes precedence over the individual fields above.
    # Note: special characters in passwords must be percent-encoded in URLs.
    redis_url: Optional[str] = None

    # ── Redis key names ──────────────────────────────────────────────────────
    queue_key: str = "flare:queue"
    stream_key: str = "flare:logs"

    # ── Storage limits ───────────────────────────────────────────────────────
    max_entries: int = 10_000
    retention_hours: int = 168  # 7 days

    # ── Dashboard ────────────────────────────────────────────────────────────
    dashboard_path: str = "/flare"
    dashboard_title: str = "Flare — Error Logs"
    dashboard_auth_dependency: Optional[Any] = Field(default=None, exclude=True)

    # ── Worker ───────────────────────────────────────────────────────────────
    worker_interval_seconds: int = 5
    worker_batch_size: int = 100

    # ── Capture options ──────────────────────────────────────────────────────
    sensitive_fields: frozenset[str] = frozenset({
        "password", "passwd", "token", "api_key", "apikey",
        "secret", "authorization", "card_number", "cvv",
        "private_key", "secret_key", "cpf", "ssn",
    })

    model_config = SettingsConfigDict(
        env_prefix="FLARE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
