from __future__ import annotations

from typing import Any, Literal, Optional

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
    # ── Storage backend ───────────────────────────────────────────────────────
    # "redis"  (default) — Redis Streams. Requires a running Redis instance.
    # "sqlite"            — Local SQLite file. Requires: pip install 'fastapi-flare[sqlite]'
    #
    # Environment variable:
    #   FLARE_STORAGE_BACKEND=sqlite
    #   FLARE_SQLITE_PATH=/data/flare.db
    storage_backend: Literal["redis", "sqlite"] = "redis"
    sqlite_path: str = "flare.db"

    # ── Metrics ──────────────────────────────────────────────────────────────
    # Maximum number of distinct endpoint keys held in the in-memory metrics
    # store. Once reached, new unknown endpoints are silently dropped to
    # prevent unbounded memory growth from scanners / URL enumeration attacks.
    metrics_max_endpoints: int = 500

    # ── Request body capture ─────────────────────────────────────────────────
    # Maximum bytes to read and store from the request body on error events.
    # Set to 0 to disable body capture entirely.
    max_request_body_bytes: int = 8192

    # ── Runtime — set by setup(), never from env ────────────────────────────────
    # The resolved storage instance; injected after make_storage() in setup().
    # Excluded from serialization and env-loading.
    storage_instance: Optional[Any] = Field(default=None, exclude=True)
    # In-memory metrics aggregator; injected by setup().
    metrics_instance: Optional[Any] = Field(default=None, exclude=True)
    # Background worker; injected by setup().
    worker_instance: Optional[Any] = Field(default=None, exclude=True)
    # ── Zitadel OAuth2 authentication (optional) ─────────────────────────────
    # When zitadel_domain + zitadel_client_id + zitadel_project_id are set,
    # setup() will automatically protect the /flare dashboard with JWT validation.
    #
    # Requires: pip install "fastapi-flare[auth]"  (httpx + python-jose)
    #
    # Environment variables (FLARE_ prefix):
    #   FLARE_ZITADEL_DOMAIN=auth.mycompany.com
    #   FLARE_ZITADEL_CLIENT_ID=000000000000000001
    #   FLARE_ZITADEL_PROJECT_ID=000000000000000002
    #   FLARE_ZITADEL_OLD_CLIENT_ID=...   # optional — legacy migration
    #   FLARE_ZITADEL_OLD_PROJECT_ID=...  # optional — legacy migration
    zitadel_domain: Optional[str] = None
    zitadel_client_id: Optional[str] = None
    zitadel_project_id: Optional[str] = None
    # Legacy / migration: tokens issued to old project IDs remain valid
    zitadel_old_client_id: Optional[str] = None
    zitadel_old_project_id: Optional[str] = None
    # When set, enables browser-based OAuth2 PKCE flow.
    # Users who open /flare in a browser are redirected to Zitadel's login page.
    # After authentication, Zitadel redirects to this URL (must point to /flare/callback).
    #
    # Example:
    #   FLARE_ZITADEL_REDIRECT_URI=https://myapp.com/flare/callback
    #
    # Without this field, the bearer-token mode is used instead (API clients only).
    zitadel_redirect_uri: Optional[str] = None
    # Secret key used to sign the session cookie in browser-based PKCE flow.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    # If not set, a random key is generated at startup (sessions lost on restart).
    #
    # Environment variable:
    #   FLARE_ZITADEL_SESSION_SECRET=<hex-string>
    zitadel_session_secret: Optional[str] = None

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
