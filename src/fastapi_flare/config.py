from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FlareConfig(BaseSettings):
    """
    Configuration for fastapi-flare.

    All fields can be set via environment variables with the ``FLARE_`` prefix,
    or loaded from a ``.env`` file automatically.

    PostgreSQL connection string examples::

        FLARE_PG_DSN=postgresql://user:pass@localhost:5432/mydb
        FLARE_PG_DSN=postgresql://user:pass@db.example.com:5432/flare?sslmode=require
    """

    # ── PostgreSQL ───────────────────────────────────────
    # Full asyncpg-compatible DSN. Required when storage_backend="postgresql".
    #
    # Environment variable:
    #   FLARE_PG_DSN=postgresql://user:pass@localhost:5432/mydb
    pg_dsn: str = "postgresql://postgres:postgres@localhost:5432/flare"

    # ── Storage limits ───────────────────────────────────────────────────────
    max_entries: int = 10_000
    retention_hours: int = 168  # 7 days
    # How often the background worker actually runs the retention DELETE.
    # The worker loop runs every worker_interval_seconds, but the expensive
    # DELETE queries only execute once per this interval to avoid overhead.
    # Default: 60 minutes. Set to 0 to run on every worker cycle.
    # Env: FLARE_RETENTION_CHECK_INTERVAL_MINUTES
    retention_check_interval_minutes: int = 60

    # ── Dashboard ────────────────────────────────────────────────────────────
    dashboard_path: str = "/flare"
    dashboard_title: str = "Flare — Error Logs"
    dashboard_auth_dependency: Optional[Any] = Field(default=None, exclude=True)
    # ── Storage backend ──────────────────────────────────────────
    # "sqlite" (default)  — Zero-config local file. Great for development and
    #                        quick testing. Requires aiosqlite.
    # "postgresql"        — Production-grade. Requires asyncpg + a running
    #                        PostgreSQL instance (set FLARE_PG_DSN).
    #
    # Environment variables:
    #   FLARE_STORAGE_BACKEND=postgresql
    #   FLARE_PG_DSN=postgresql://user:pass@localhost:5432/mydb
    storage_backend: Literal["postgresql", "sqlite"] = "sqlite"
    sqlite_path: str = "flare.db"
    # Name of the PostgreSQL table used to store logs.
    # Change this per-project to share one database across multiple APIs:
    #   FLARE_PG_TABLE_NAME=flare_logs_checkout
    #   FLARE_PG_TABLE_NAME=flare_logs_auth
    # Only alphanumeric characters and underscores are allowed.
    pg_table_name: str = "flare_logs"

    # ── Metrics ──────────────────────────────────────────────────────────────
    # Maximum number of distinct endpoint keys held in the in-memory metrics
    # store. Once reached, new unknown endpoints are silently dropped to
    # prevent unbounded memory growth from scanners / URL enumeration attacks.
    metrics_max_endpoints: int = 500

    # Persist the in-memory metrics snapshot to the storage backend so that
    # multiple uvicorn workers / pods can see each other's aggregates and the
    # dashboard survives process restarts.
    # Off by default — single-process dev setups don't need it.
    # Env: FLARE_METRICS_PERSISTENCE=true
    metrics_persistence: bool = False

    # How often the worker persists this process's FlareMetrics snapshot.
    # Env: FLARE_METRICS_FLUSH_INTERVAL_SECONDS
    metrics_flush_interval_seconds: int = 30

    # Snapshots older than this are treated as belonging to a crashed worker
    # and ignored when merging at dashboard render time.
    # Env: FLARE_METRICS_SNAPSHOT_TTL_SECONDS
    metrics_snapshot_ttl_seconds: int = 180

    # ── Request body capture ─────────────────────────────────────────────────
    # Maximum bytes to read and store from the request body on error events.
    # Set to 0 to disable body capture entirely.
    max_request_body_bytes: int = 8192

    # ── Response body capture ────────────────────────────────────────────────
    # Opt-in snapshot of what the endpoint RESPONDED with. Useful to debug
    # "why did the client receive this?" without leaving your observability stack.
    #
    # Default: off. Only JSON/text responses are captured; binary / streaming
    # responses (image/*, video/*, application/octet-stream, text/event-stream,
    # StreamingResponse, FileResponse) are silently skipped so downloads and SSE
    # keep working.
    #
    # Privacy: values whose key matches ``sensitive_fields`` are redacted before
    # storage, same as for request bodies.
    #
    # Storage cost is bounded by the existing retention mechanisms PLUS an
    # additional null-out step that drops the payload column after
    # ``response_body_retention_hours`` — the row itself stays (for metrics),
    # only the large JSON gets cleared.
    #
    # Env: FLARE_CAPTURE_RESPONSE_BODY=true
    capture_response_body: bool = False
    # Only capture responses at or above this HTTP status.
    # Default 400 = errors only (typical debug use case).
    # Set to 200 to capture every response (higher volume).
    # Env: FLARE_CAPTURE_RESPONSE_BODY_MIN_STATUS=400
    capture_response_body_min_status: int = 400
    # Maximum bytes to store per response body.
    # Env: FLARE_MAX_RESPONSE_BODY_BYTES=8192
    max_response_body_bytes: int = 8192
    # Hours after which the ``response_body`` column is NULLed out (row stays).
    # Run on the worker's retention cycle. Default 24h — user-controlled short TTL
    # aligned with the use case of post-mortem debugging only.
    # Env: FLARE_RESPONSE_BODY_RETENTION_HOURS=24
    response_body_retention_hours: int = 24

    # ── Request tracking (ring buffer) ────────────────────────────────────────
    # Enables the /flare/requests dashboard tab.
    # All 4xx/5xx are always stored. 2xx requests are opt-in via track_2xx_requests.
    # Env: FLARE_TRACK_REQUESTS=true
    track_requests: bool = True
    # Ring buffer size — oldest entries are deleted when limit is exceeded.
    # Env: FLARE_REQUEST_MAX_ENTRIES=1000
    request_max_entries: int = 1000
    # Also store 2xx (successful) requests. Off by default to keep volume low.
    # Env: FLARE_TRACK_2XX_REQUESTS=true
    track_2xx_requests: bool = False
    # Store request headers per entry. Off by default (adds data volume).
    # Env: FLARE_CAPTURE_REQUEST_HEADERS=true
    capture_request_headers: bool = False

    # ── Request tracking — batch insert (high throughput) ────────────────────
    # When > 0, RequestTrackingMiddleware appends each entry to an in-memory
    # buffer instead of issuing one INSERT per request. The worker flushes
    # the buffer in a single multi-row INSERT (executemany) every
    # ``request_buffer_flush_seconds`` or whenever the buffer reaches this
    # size, whichever comes first.
    #
    # Trade-off: lower per-request latency (no DB hit on the hot path) at the
    # cost of a small visibility lag — the dashboard's HTTP Requests tab will
    # not see in-flight entries until the next flush.
    #
    # Default 0 = immediate write per request (safest for low-volume / dev).
    # 100–500 is a good range for sustained >50 req/s deployments.
    # Env: FLARE_REQUEST_BUFFER_SIZE
    request_buffer_size: int = 0
    # Maximum delay between buffer flushes when the size threshold isn't hit.
    # Env: FLARE_REQUEST_BUFFER_FLUSH_SECONDS
    request_buffer_flush_seconds: int = 2

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

    # ── Alerts / Notifiers ───────────────────────────────────────────────────
    # List of notifier instances (SlackNotifier, DiscordNotifier, TeamsNotifier,
    # WebhookNotifier, or any object with an async send(entry: dict) method).
    # When non-empty, a background task fires each notifier whenever a log entry
    # whose level is >= alert_min_level is captured.
    #
    # Example:
    #   from fastapi_flare.notifiers import SlackNotifier
    #   alert_notifiers=[SlackNotifier("https://hooks.slack.com/services/...")]
    alert_notifiers: list[Any] = Field(default_factory=list, exclude=True)

    # Minimum severity level that triggers a notification.
    # "ERROR"   — only unhandled 5xx / exceptions (default, quieter)
    # "WARNING" — also includes 4xx HTTP errors
    alert_min_level: str = "ERROR"

    # Minimum seconds between alerts for the same (event, endpoint) fingerprint.
    # Prevents alert fatigue when the same error is repeating rapidly.
    # Set to 0 to disable deduplication.
    alert_cooldown_seconds: int = 300

    # ── Runtime alert dedup cache (never from env) ────────────────────────────
    # Dict[fingerprint_str, last_sent_timestamp] — populated at runtime only.
    alert_cache_instance: dict = Field(default_factory=dict, exclude=True)

    # ── Worker ───────────────────────────────────────────────────────────────
    worker_interval_seconds: int = 5
    worker_batch_size: int = 100

    # ── Non-HTTP error capture ───────────────────────────────────────────────
    # Forward records from Python's ``logging`` module (WARNING+) to Flare.
    # Captures errors from background tasks, workers, cron jobs — anything
    # that uses ``logger.error`` / ``logger.exception`` outside a request.
    # Env: FLARE_CAPTURE_LOGGING=true
    capture_logging: bool = False

    # Comma-separated list of logger names to attach to. Empty = root logger,
    # which catches every propagated record.
    # Env: FLARE_CAPTURE_LOGGING_LOGGERS=myapp.worker,myapp.jobs
    capture_logging_loggers: Optional[str] = None

    # Install an asyncio event-loop exception handler that captures
    # unhandled errors from fire-and-forget tasks.
    # Env: FLARE_CAPTURE_ASYNCIO_ERRORS=true
    capture_asyncio_errors: bool = False

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
