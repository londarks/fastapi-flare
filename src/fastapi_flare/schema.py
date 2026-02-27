from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel


class FlareLogEntry(BaseModel):
    """
    One captured log entry returned by the REST API.

    The ``id`` field is the storage backend's native identifier:
      - PostgreSQL: the BIGSERIAL primary key as a string.
      - SQLite: the AUTOINCREMENT row id as a string.
    """

    id: str
    timestamp: datetime
    level: Literal["ERROR", "WARNING"]
    event: str
    message: str
    request_id: Optional[str] = None
    endpoint: Optional[str] = None
    http_method: Optional[str] = None
    http_status: Optional[int] = None
    ip_address: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    stack_trace: Optional[str] = None
    context: Optional[dict] = None
    request_body: Optional[Any] = None

    model_config = {"from_attributes": True}


class FlareLogPage(BaseModel):
    """Paginated response returned by GET /flare/api/logs."""

    logs: list[FlareLogEntry]
    total: int
    page: int
    limit: int
    pages: int


class FlareEndpointMetric(BaseModel):
    """Per-endpoint request metrics aggregated in memory by FlareMetrics."""

    endpoint: str
    count: int
    errors: int
    avg_latency_ms: int
    p95_latency_ms: int = 0
    max_latency_ms: int
    error_rate: float


class FlareMetricsSnapshot(BaseModel):
    """Snapshot of all endpoint metrics returned by GET /flare/api/metrics."""

    endpoints: list[FlareEndpointMetric]
    total_requests: int
    total_errors: int
    at_capacity: bool = False
    max_endpoints: int = 500


class FlareStats(BaseModel):
    """Summary statistics returned by GET /flare/api/stats."""

    total_entries: int
    errors_last_24h: int
    warnings_last_24h: int
    queue_length: int      # always 0 for direct-write backends
    stream_length: int     # total rows in the storage table
    oldest_entry_ts: Optional[datetime] = None
    newest_entry_ts: Optional[datetime] = None


class FlareStorageActionResult(BaseModel):
    """Response returned by storage maintenance endpoints (trim, clear)."""

    ok: bool
    action: str
    detail: str = ""


class FlareStorageOverview(BaseModel):
    """Runtime snapshot of the active storage backend for GET /flare/api/storage/overview."""

    backend: str
    connected: bool
    error: Optional[str] = None
    # Retention config (always present)
    max_entries: int = 0
    retention_hours: int = 0
    # Backend-specific live counters
    row_count: Optional[int] = None
    # PostgreSQL-specific
    pg_version: Optional[str] = None
    pool_size: Optional[int] = None
    pool_idle: Optional[int] = None
    dsn: Optional[str] = None
    # SQLite-specific
    db_path: Optional[str] = None
    file_size_bytes: Optional[int] = None
    wal_active: Optional[bool] = None


class FlareRequestEntry(BaseModel):
    """
    One captured HTTP request stored in the ring buffer.

    Linked to ``FlareLogEntry`` via ``request_id`` — if the request caused
    an error, the same ``request_id`` appears in ``flare_logs``.
    """

    id: str
    timestamp: datetime
    method: str
    path: str
    status_code: int
    duration_ms: Optional[int] = None
    request_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_headers: Optional[dict] = None
    request_body: Optional[Any] = None
    # linked error entry id (populated by the router when joining)
    error_id: Optional[str] = None

    model_config = {"from_attributes": True}


class FlareRequestPage(BaseModel):
    """Paginated response for GET /flare/api/requests."""

    requests: list[FlareRequestEntry]
    total: int
    page: int
    limit: int
    pages: int


class FlareRequestStats(BaseModel):
    """Summary stats for the requests ring buffer."""

    total_stored: int               # rows currently in the ring buffer
    ring_buffer_size: int           # configured max
    requests_last_hour: int
    errors_last_hour: int           # 4xx + 5xx in the last hour
    avg_duration_ms: Optional[int] = None
    slowest_endpoint: Optional[str] = None
    slowest_duration_ms: Optional[int] = None


class FlareHealthReport(BaseModel):
    """Health report returned by GET /flare/health."""

    status: Literal["ok", "degraded", "down"]
    storage_backend: str
    storage: Literal["ok", "error"]
    storage_error: Optional[str] = None
    worker_running: bool
    worker_flush_cycles: int
    queue_size: int
    uptime_seconds: Optional[int] = None
