"""
PostgreSQL backend for fastapi-flare.
======================================

Implements :class:`~fastapi_flare.storage.base.FlareStorageProtocol` backed
by a PostgreSQL database using ``asyncpg`` for non-blocking async I/O.

Design decisions
----------------
* **Connection pool** — ``asyncpg.create_pool()`` is used (min=1, max=10) so
  concurrent requests never queue waiting for a single connection.
* **Direct writes** — ``enqueue()`` performs an immediate INSERT.  No separate
  buffer or drain step is needed (unlike the former Redis List approach).
* **Lazy init** — the pool and DDL migrations run on the first operation so
  startup remains fast.
* **JSONB columns** — ``context`` and ``request_body`` are stored as JSONB for
  efficient querying if needed in future.
* **Retention** — ``flush()`` runs DELETE + cap enforcement; called by the
  background worker on its normal interval.

Requires::

    pip install asyncpg
    # or full install: pip install 'fastapi-flare[postgresql]'

Connection string examples::

    postgresql://user:password@localhost:5432/mydb
    postgresql://user:password@db.example.com:5432/flare?sslmode=require
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi_flare.schema import (
    FlareIssue,
    FlareIssueStats,
    FlareLogEntry,
    FlareRequestEntry,
    FlareRequestStats,
    FlareStats,
)

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


# ── Schema ────────────────────────────────────────────────────────────────────

def _build_ddl(table: str) -> str:
    """Generate CREATE TABLE + indexes DDL for the given table name."""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id                BIGSERIAL    PRIMARY KEY,
    entry_id          TEXT         NOT NULL DEFAULT '',
    timestamp         TIMESTAMPTZ  NOT NULL,
    level             TEXT         NOT NULL,
    event             TEXT         NOT NULL,
    message           TEXT         NOT NULL DEFAULT '',
    endpoint          TEXT,
    http_method       TEXT,
    http_status       INTEGER,
    duration_ms       INTEGER,
    request_id        TEXT,
    issue_fingerprint TEXT,
    ip_address        TEXT,
    error             TEXT,
    stack_trace       TEXT,
    context           JSONB,
    request_body      JSONB
);

-- Migration: add issue_fingerprint to existing deployments
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS issue_fingerprint TEXT;

-- Single-column indexes for basic filtering and ORDER BY
CREATE INDEX IF NOT EXISTS idx_{table}_timestamp  ON {table} (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_{table}_level      ON {table} (level);
CREATE INDEX IF NOT EXISTS idx_{table}_endpoint   ON {table} (endpoint);
CREATE INDEX IF NOT EXISTS idx_{table}_event      ON {table} (event);
CREATE INDEX IF NOT EXISTS idx_{table}_http_status ON {table} (http_status);
CREATE INDEX IF NOT EXISTS idx_{table}_issue_fp   ON {table} (issue_fingerprint, timestamp DESC);

-- Composite index: covers get_stats queries (level filter + time range in one scan)
-- e.g. COUNT(*) FILTER (WHERE level = 'ERROR' AND timestamp > $1)
CREATE INDEX IF NOT EXISTS idx_{table}_level_ts   ON {table} (level, timestamp DESC);
"""


def _build_issues_ddl(table: str) -> str:
    """Generate CREATE TABLE + indexes DDL for the grouped-issues store."""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    fingerprint       TEXT        PRIMARY KEY,
    exception_type    TEXT,
    endpoint          TEXT,
    sample_message    TEXT        NOT NULL DEFAULT '',
    sample_request_id TEXT,
    occurrence_count  BIGINT      NOT NULL DEFAULT 1,
    first_seen        TIMESTAMPTZ NOT NULL,
    last_seen         TIMESTAMPTZ NOT NULL,
    level             TEXT        NOT NULL,
    resolved          BOOLEAN     NOT NULL DEFAULT FALSE,
    resolved_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_{table}_last_seen          ON {table} (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_{table}_resolved_last_seen ON {table} (resolved, last_seen DESC);
"""


def _build_settings_ddl(table: str) -> str:
    """Generate CREATE TABLE DDL for the key-value settings store."""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '{{}}'
);
"""


def _build_metrics_ddl(table: str) -> str:
    """Generate CREATE TABLE + index DDL for the metrics snapshot store."""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    worker_id  TEXT        PRIMARY KEY,
    updated_at TIMESTAMPTZ NOT NULL,
    payload    JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_{table}_updated
    ON {table} (updated_at DESC);
"""


def _build_requests_ddl(table: str) -> str:
    """Generate CREATE TABLE + indexes DDL for the HTTP requests ring-buffer table."""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id              BIGSERIAL    PRIMARY KEY,
    timestamp       TIMESTAMPTZ  NOT NULL,
    method          TEXT         NOT NULL,
    path            TEXT         NOT NULL,
    status_code     INTEGER      NOT NULL,
    duration_ms     INTEGER,
    request_id      TEXT,
    ip_address      TEXT,
    user_agent      TEXT,
    request_headers JSONB,
    request_body    JSONB,
    error_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_{table}_timestamp   ON {table} (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_{table}_status_code ON {table} (status_code);
CREATE INDEX IF NOT EXISTS idx_{table}_method      ON {table} (method);
CREATE INDEX IF NOT EXISTS idx_{table}_path        ON {table} (path);
CREATE INDEX IF NOT EXISTS idx_{table}_request_id  ON {table} (request_id);
CREATE INDEX IF NOT EXISTS idx_{table}_status_ts   ON {table} (status_code, timestamp DESC);
"""


# ── Backend ───────────────────────────────────────────────────────────────────

class PostgreSQLStorage:
    """
    ``FlareStorageProtocol`` implementation backed by PostgreSQL via asyncpg.

    The write path is fully async and direct — every :meth:`enqueue` call
    performs an immediate INSERT, so no separate buffer-drain step is required.
    :meth:`flush` is used exclusively for retention cleanup (time-based +
    count-based cap).

    Suitable for:
      - Production deployments that already run PostgreSQL.
      - Multi-process / multi-instance setups (shared persistent storage).
      - Workloads that need durable, crash-safe log storage.
    """

    def __init__(self, config: "FlareConfig") -> None:
        self._config = config
        self._pool: Any = None  # asyncpg.Pool, created on first use
        self._last_retention_at: Optional[datetime] = None  # throttle for flush()

    @property
    def _table(self) -> str:
        """Resolved table name — safe: config-controlled, validated on startup."""
        return self._config.pg_table_name

    @property
    def _requests_table(self) -> str:
        """Derived requests table name, e.g. ``flare_logs`` → ``flare_requests``."""
        base = self._table
        return base.replace("_logs", "_requests") if "_logs" in base else base + "_requests"

    @property
    def _settings_table(self) -> str:
        """Derived settings table name, e.g. ``flare_logs`` → ``flare_settings``."""
        base = self._table
        return base.replace("_logs", "_settings") if "_logs" in base else base + "_settings"

    @property
    def _metrics_table(self) -> str:
        """Derived metrics snapshot table, e.g. ``flare_logs`` → ``flare_metrics``."""
        base = self._table
        return base.replace("_logs", "_metrics") if "_logs" in base else base + "_metrics"

    @property
    def _issues_table(self) -> str:
        """Derived issues table, e.g. ``flare_logs`` → ``flare_issues``."""
        base = self._table
        return base.replace("_logs", "_issues") if "_logs" in base else base + "_issues"

    # ── Lazy pool init ────────────────────────────────────────────────────────

    async def _ensure_pool(self) -> Any:
        """Create the asyncpg connection pool and run DDL migrations on first call."""
        if self._pool is not None:
            return self._pool

        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "asyncpg is required for the PostgreSQL storage backend. "
                "Install it with: pip install asyncpg"
            ) from exc

        self._pool = await asyncpg.create_pool(
            dsn=self._config.pg_dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )

        async with self._pool.acquire() as conn:
            await conn.execute(_build_ddl(self._table))
            await conn.execute(_build_requests_ddl(self._requests_table))
            await conn.execute(_build_settings_ddl(self._settings_table))
            await conn.execute(_build_metrics_ddl(self._metrics_table))
            await conn.execute(_build_issues_ddl(self._issues_table))

        return self._pool

    # ── Write path ────────────────────────────────────────────────────────────

    async def enqueue(self, entry_dict: dict) -> None:
        """
        INSERT one log entry directly into ``flare_logs``.
        Never raises — any failure is silently discarded.
        """
        try:
            pool = await self._ensure_pool()

            ctx = entry_dict.get("context")
            body = entry_dict.get("request_body")
            ctx_val = json.dumps(ctx, default=str) if isinstance(ctx, (dict, list)) else None
            body_val = json.dumps(body, default=str) if isinstance(body, (dict, list)) else None

            # asyncpg accepts datetime objects for TIMESTAMPTZ columns directly.
            ts_raw = entry_dict.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    ts = datetime.now(tz=timezone.utc)
            elif isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                ts = datetime.now(tz=timezone.utc)

            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {self._table} (
                        timestamp, level, event, message, endpoint,
                        http_method, http_status, duration_ms, request_id,
                        issue_fingerprint,
                        ip_address, error, stack_trace, context, request_body
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9,
                        $10,
                        $11, $12, $13, $14::jsonb, $15::jsonb
                    )
                    """,
                    ts,
                    entry_dict.get("level", "ERROR"),
                    entry_dict.get("event", "unknown"),
                    entry_dict.get("message", "") or "",
                    entry_dict.get("endpoint"),
                    entry_dict.get("http_method"),
                    entry_dict.get("http_status"),
                    entry_dict.get("duration_ms"),
                    entry_dict.get("request_id"),
                    entry_dict.get("issue_fingerprint"),
                    entry_dict.get("ip_address"),
                    entry_dict.get("error"),
                    entry_dict.get("stack_trace"),
                    ctx_val,
                    body_val,
                )
        except Exception:  # noqa: BLE001
            pass

    # ── Maintenance ───────────────────────────────────────────────────────────

    async def flush(self) -> None:
        """
        Apply retention policies. Called every ``worker_interval_seconds``
        by the background worker, but the actual DELETEs run at most once
        every ``retention_check_interval_minutes`` (default 60 min) to avoid
        unnecessary database load.

        Steps:
          1. Delete rows older than ``retention_hours`` (default 7 days).
          2. Delete the oldest rows exceeding ``max_entries`` (count-based cap).
        """
        config = self._config
        interval = config.retention_check_interval_minutes
        now = datetime.now(tz=timezone.utc)

        if interval > 0 and self._last_retention_at is not None:
            elapsed = (now - self._last_retention_at).total_seconds() / 60
            if elapsed < interval:
                return  # not time yet — skip this cycle

        try:
            pool = await self._ensure_pool()
            cutoff = now - timedelta(hours=config.retention_hours)

            async with pool.acquire() as conn:
                # Time-based retention
                await conn.execute(
                    f"DELETE FROM {self._table} WHERE timestamp < $1",
                    cutoff,
                )
                # Count-based cap: keep only the newest max_entries rows
                await conn.execute(
                    f"""
                    DELETE FROM {self._table}
                    WHERE id NOT IN (
                        SELECT id FROM {self._table}
                        ORDER BY timestamp DESC
                        LIMIT $1
                    )
                    """,
                    config.max_entries,
                )

            self._last_retention_at = now
        except Exception:  # noqa: BLE001
            pass

    async def health(self) -> tuple[bool, str, int]:
        """
        Execute ``SELECT 1`` to verify the pool is reachable.
        Returns ``(ok, error_msg, queue_size=0)``.
        """
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True, "", 0
        except Exception as exc:
            return False, str(exc), 0

    async def clear(self) -> tuple[bool, str]:
        """Delete all log entries from ``flare_logs``. Returns ``(ok, detail)``."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(f"DELETE FROM {self._table}")
            # asyncpg returns the command tag, e.g. "DELETE 123"
            count = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
            return True, f"Deleted {count} row(s)"
        except Exception as exc:
            return False, str(exc)

    async def overview(self) -> dict:
        """Return a runtime snapshot dict for the storage dashboard."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                row_count = await conn.fetchval(f"SELECT COUNT(*) FROM {self._table}") or 0
                pg_version = await conn.fetchval("SELECT version()")
                pool_size = pool.get_size()
                pool_idle = pool.get_idle_size()
            return {
                "connected":   True,
                "row_count":   row_count,
                "pg_version":  pg_version,
                "pool_size":   pool_size,
                "pool_idle":   pool_idle,
                "dsn":         _mask_dsn(self._config.pg_dsn),
            }
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

    async def close(self) -> None:
        """Close all connections in the pool."""
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:  # noqa: BLE001
                pass
            self._pool = None

    # ── Request tracking ──────────────────────────────────────────────────────

    async def enqueue_request(self, entry_dict: dict) -> None:
        """
        INSERT one HTTP request entry and immediately enforce the ring-buffer cap
        by deleting any rows beyond ``request_max_entries`` (oldest first).
        Both operations run inside a single transaction.
        """
        if not self._config.track_requests:
            return
        try:
            pool = await self._ensure_pool()

            ts_raw = entry_dict.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    ts = datetime.now(tz=timezone.utc)
            elif isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                ts = datetime.now(tz=timezone.utc)

            headers = entry_dict.get("request_headers")
            body = entry_dict.get("request_body")
            headers_val = json.dumps(headers, default=str) if isinstance(headers, (dict, list)) else None
            body_val = json.dumps(body, default=str) if isinstance(body, (dict, list)) else None

            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        f"""
                        INSERT INTO {self._requests_table} (
                            timestamp, method, path, status_code, duration_ms,
                            request_id, ip_address, user_agent,
                            request_headers, request_body, error_id
                        ) VALUES (
                            $1, $2, $3, $4, $5,
                            $6, $7, $8,
                            $9::jsonb, $10::jsonb, $11
                        )
                        """,
                        ts,
                        entry_dict.get("method", "GET"),
                        entry_dict.get("path", "/"),
                        entry_dict.get("status_code", 200),
                        entry_dict.get("duration_ms"),
                        entry_dict.get("request_id"),
                        entry_dict.get("ip_address"),
                        entry_dict.get("user_agent"),
                        headers_val,
                        body_val,
                        entry_dict.get("error_id"),
                    )
                    # Ring-buffer enforcement: keep only the newest N rows
                    await conn.execute(
                        f"""
                        DELETE FROM {self._requests_table}
                        WHERE id NOT IN (
                            SELECT id FROM {self._requests_table}
                            ORDER BY timestamp DESC
                            LIMIT $1
                        )
                        """,
                        self._config.request_max_entries,
                    )
        except Exception:  # noqa: BLE001
            pass

    async def list_requests(
        self,
        *,
        page: int = 1,
        limit: int = 50,
        method: Optional[str] = None,
        status_code: Optional[int] = None,
        path: Optional[str] = None,
        min_duration_ms: Optional[int] = None,
    ) -> tuple[list[FlareRequestEntry], int]:
        """SELECT with optional filters, ordered newest-first, with pagination."""
        try:
            pool = await self._ensure_pool()
        except Exception:
            return [], 0

        clauses: list[str] = []
        params: list[Any] = []
        idx = 1

        if method:
            clauses.append(f"method = ${idx}")
            params.append(method.upper())
            idx += 1
        if status_code is not None:
            clauses.append(f"status_code = ${idx}")
            params.append(status_code)
            idx += 1
        if path:
            clauses.append(f"path ILIKE ${idx}")
            params.append(f"%{path}%")
            idx += 1
        if min_duration_ms is not None:
            clauses.append(f"duration_ms >= ${idx}")
            params.append(min_duration_ms)
            idx += 1

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        offset = (page - 1) * limit

        try:
            async with pool.acquire() as conn:
                total: int = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {self._requests_table} {where}",
                    *params,
                ) or 0

                rows = await conn.fetch(
                    f"""
                    SELECT * FROM {self._requests_table} {where}
                    ORDER BY timestamp DESC
                    LIMIT ${idx} OFFSET ${idx + 1}
                    """,
                    *params,
                    limit,
                    offset,
                )

            return [_row_to_request_entry(row) for row in rows], total
        except Exception:
            return [], 0

    async def get_request_stats(self) -> FlareRequestStats:
        """Return ring-buffer stats and aggregated metrics."""
        try:
            pool = await self._ensure_pool()
        except Exception:
            return _empty_request_stats(self._config.request_max_entries)

        try:
            cutoff_1h = datetime.now(tz=timezone.utc) - timedelta(hours=1)
            rt = self._requests_table
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT
                        COUNT(*)                                                                AS total_stored,
                        COUNT(*) FILTER (WHERE timestamp > $1)                                 AS requests_last_hour,
                        COUNT(*) FILTER (WHERE status_code >= 400 AND timestamp > $1)          AS errors_last_hour,
                        AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL)::int           AS avg_duration_ms,
                        MAX(duration_ms)                                                       AS slowest_duration_ms
                    FROM {rt}
                    """,
                    cutoff_1h,
                )
                # Slowest endpoint
                slowest_row = await conn.fetchrow(
                    f"""
                    SELECT path
                    FROM {rt}
                    WHERE duration_ms = (SELECT MAX(duration_ms) FROM {rt})
                    LIMIT 1
                    """
                )

            return FlareRequestStats(
                total_stored=row["total_stored"] or 0,
                ring_buffer_size=self._config.request_max_entries,
                requests_last_hour=row["requests_last_hour"] or 0,
                errors_last_hour=row["errors_last_hour"] or 0,
                avg_duration_ms=row["avg_duration_ms"],
                slowest_endpoint=slowest_row["path"] if slowest_row else None,
                slowest_duration_ms=row["slowest_duration_ms"],
            )
        except Exception:
            return _empty_request_stats(self._config.request_max_entries)
    # ── Settings ──────────────────────────────────────────────────────────────

    async def get_settings(self, key: str) -> dict:
        """Return the stored settings dict for *key*, or {} if not found."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT value FROM {self._settings_table} WHERE key = $1", key
                )
            if row:
                return json.loads(row["value"])
        except Exception:
            pass
        return {}

    async def save_settings(self, key: str, value: dict) -> None:
        """Upsert *value* JSON under *key* in the settings table."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {self._settings_table} (key, value) VALUES ($1, $2)"
                    f" ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                    key,
                    json.dumps(value, default=str),
                )
        except Exception:
            pass

    # ── Metrics snapshots ──────────────────────────────────────────────────

    async def flush_metrics(self, worker_id: str, payload: dict) -> None:
        """Upsert this worker's latest FlareMetrics snapshot."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {self._metrics_table}"
                    f" (worker_id, updated_at, payload) VALUES ($1, $2, $3::jsonb)"
                    f" ON CONFLICT(worker_id) DO UPDATE SET"
                    f"   updated_at = EXCLUDED.updated_at,"
                    f"   payload    = EXCLUDED.payload",
                    worker_id,
                    datetime.now(tz=timezone.utc),
                    json.dumps(payload, default=str),
                )
        except Exception:
            pass

    async def load_metrics_snapshots(
        self, *, since_seconds: int
    ) -> list[tuple[str, dict]]:
        """Return every snapshot updated within the last *since_seconds*."""
        try:
            pool = await self._ensure_pool()
            cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=since_seconds)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT worker_id, payload FROM {self._metrics_table}"
                    f" WHERE updated_at >= $1",
                    cutoff,
                )
            out: list[tuple[str, dict]] = []
            for r in rows:
                raw = r["payload"]
                try:
                    out.append(
                        (r["worker_id"], raw if isinstance(raw, dict) else json.loads(raw))
                    )
                except Exception:
                    continue
            return out
        except Exception:
            return []

    # ── Read path ─────────────────────────────────────────────────────────────

    async def list_logs(
        self,
        *,
        page: int = 1,
        limit: int = 50,
        level: Optional[str] = None,
        event: Optional[str] = None,
        search: Optional[str] = None,
    ) -> tuple[list[FlareLogEntry], int]:
        """
        SELECT with optional filters, ordered newest-first, with pagination.
        Returns ``(entries_for_page, total_matching)``.
        """
        try:
            pool = await self._ensure_pool()
        except Exception:
            return [], 0

        clauses: list[str] = []
        params: list[Any] = []
        idx = 1

        if level:
            clauses.append(f"level = ${idx}")
            params.append(level)
            idx += 1
        if event:
            clauses.append(f"event ILIKE ${idx}")
            params.append(f"%{event}%")
            idx += 1
        if search:
            clauses.append(f"(message ILIKE ${idx} OR error ILIKE ${idx + 1})")
            params.append(f"%{search}%")
            params.append(f"%{search}%")
            idx += 2

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        offset = (page - 1) * limit

        try:
            async with pool.acquire() as conn:
                total: int = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {self._table} {where}",
                    *params,
                ) or 0

                rows = await conn.fetch(
                    f"""
                    SELECT * FROM {self._table} {where}
                    ORDER BY timestamp DESC
                    LIMIT ${idx} OFFSET ${idx + 1}
                    """,
                    *params,
                    limit,
                    offset,
                )

            return [_row_to_entry(row) for row in rows], total
        except Exception:
            return [], 0

    # ── Issue grouping ─────────────────────────────────────────────────────

    async def upsert_issue(
        self,
        *,
        fingerprint: str,
        exception_type: Optional[str],
        endpoint: Optional[str],
        sample_message: str,
        sample_request_id: Optional[str],
        level: str,
        timestamp: datetime,
    ) -> None:
        try:
            pool = await self._ensure_pool()
            tbl = self._issues_table
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {tbl} (
                        fingerprint, exception_type, endpoint,
                        sample_message, sample_request_id,
                        occurrence_count, first_seen, last_seen,
                        level, resolved, resolved_at
                    ) VALUES ($1, $2, $3, $4, $5, 1, $6, $6, $7, FALSE, NULL)
                    ON CONFLICT (fingerprint) DO UPDATE SET
                        occurrence_count = {tbl}.occurrence_count + 1,
                        last_seen = GREATEST({tbl}.last_seen, EXCLUDED.last_seen),
                        level = CASE
                            WHEN EXCLUDED.level = 'ERROR' THEN 'ERROR'
                            ELSE {tbl}.level
                        END,
                        resolved = FALSE,
                        resolved_at = NULL
                    """,
                    fingerprint,
                    exception_type,
                    endpoint,
                    (sample_message or "")[:500],
                    sample_request_id,
                    timestamp,
                    level,
                )
        except Exception:  # noqa: BLE001
            pass

    async def list_issues(
        self,
        *,
        page: int = 1,
        limit: int = 50,
        resolved: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> tuple[list[FlareIssue], int]:
        try:
            pool = await self._ensure_pool()
        except Exception:
            return [], 0

        clauses: list[str] = []
        params: list[Any] = []
        idx = 1
        if resolved is not None:
            clauses.append(f"resolved = ${idx}")
            params.append(resolved)
            idx += 1
        if search:
            clauses.append(
                f"(exception_type ILIKE ${idx} OR endpoint ILIKE ${idx + 1} OR sample_message ILIKE ${idx + 2})"
            )
            params.extend([f"%{search}%"] * 3)
            idx += 3

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        offset = (page - 1) * limit
        tbl = self._issues_table

        try:
            async with pool.acquire() as conn:
                total: int = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {tbl} {where}", *params
                ) or 0
                rows = await conn.fetch(
                    f"""
                    SELECT * FROM {tbl} {where}
                    ORDER BY last_seen DESC
                    LIMIT ${idx} OFFSET ${idx + 1}
                    """,
                    *params,
                    limit,
                    offset,
                )
            return [_row_to_issue(r) for r in rows], total
        except Exception:
            return [], 0

    async def get_issue(self, fingerprint: str) -> Optional[FlareIssue]:
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT * FROM {self._issues_table} WHERE fingerprint = $1",
                    fingerprint,
                )
            return _row_to_issue(row) if row else None
        except Exception:
            return None

    async def list_logs_for_issue(
        self,
        fingerprint: str,
        *,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[FlareLogEntry], int]:
        try:
            pool = await self._ensure_pool()
        except Exception:
            return [], 0

        offset = (page - 1) * limit
        try:
            async with pool.acquire() as conn:
                total: int = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {self._table} WHERE issue_fingerprint = $1",
                    fingerprint,
                ) or 0
                rows = await conn.fetch(
                    f"""
                    SELECT * FROM {self._table}
                    WHERE issue_fingerprint = $1
                    ORDER BY timestamp DESC
                    LIMIT $2 OFFSET $3
                    """,
                    fingerprint,
                    limit,
                    offset,
                )
            return [_row_to_entry(r) for r in rows], total
        except Exception:
            return [], 0

    async def update_issue_status(
        self, fingerprint: str, *, resolved: bool
    ) -> bool:
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(
                    f"""
                    UPDATE {self._issues_table}
                    SET resolved = $2,
                        resolved_at = CASE WHEN $2 THEN NOW() ELSE NULL END
                    WHERE fingerprint = $1
                    """,
                    fingerprint,
                    resolved,
                )
            # asyncpg returns a command tag like "UPDATE 1"
            try:
                return int(result.rsplit(" ", 1)[-1]) > 0
            except (ValueError, IndexError):
                return False
        except Exception:
            return False

    async def get_issue_stats(self) -> FlareIssueStats:
        try:
            pool = await self._ensure_pool()
        except Exception:
            return FlareIssueStats()

        try:
            cutoff_24h = datetime.now(tz=timezone.utc) - timedelta(hours=24)
            cutoff_7d = datetime.now(tz=timezone.utc) - timedelta(days=7)
            tbl = self._issues_table
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT
                        COUNT(*)                                                       AS total,
                        COUNT(*) FILTER (WHERE NOT resolved)                            AS open,
                        COUNT(*) FILTER (WHERE resolved)                                AS resolved,
                        COUNT(*) FILTER (WHERE first_seen > $1)                         AS new_24h,
                        COUNT(*) FILTER (WHERE resolved AND resolved_at > $2)           AS resolved_7d
                    FROM {tbl}
                    """,
                    cutoff_24h,
                    cutoff_7d,
                )
            return FlareIssueStats(
                total=row["total"] or 0,
                open=row["open"] or 0,
                resolved=row["resolved"] or 0,
                new_last_24h=row["new_24h"] or 0,
                resolved_last_7d=row["resolved_7d"] or 0,
            )
        except Exception:
            return FlareIssueStats()

    async def get_stats(self) -> FlareStats:
        """
        Retrieve summary statistics via efficient COUNT queries.
        ``queue_length`` is always 0 — writes are direct, no buffer.
        """
        try:
            pool = await self._ensure_pool()
        except Exception:
            return _empty_stats()

        try:
            cutoff_24h = datetime.now(tz=timezone.utc) - timedelta(hours=24)
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*)                                                           AS total,
                        COUNT(*) FILTER (WHERE level = 'ERROR'   AND timestamp > $1)      AS errors_24h,
                        COUNT(*) FILTER (WHERE level = 'WARNING' AND timestamp > $1)      AS warnings_24h,
                        MIN(timestamp)                                                     AS oldest_ts,
                        MAX(timestamp)                                                     AS newest_ts
                    FROM {self._table}
                    """,
                    cutoff_24h,
                )

            return FlareStats(
                total_entries=row["total"] or 0,
                errors_last_24h=row["errors_24h"] or 0,
                warnings_last_24h=row["warnings_24h"] or 0,
                queue_length=0,
                stream_length=row["total"] or 0,
                oldest_entry_ts=row["oldest_ts"],
                newest_entry_ts=row["newest_ts"],
            )
        except Exception:
            return _empty_stats()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_stats() -> FlareStats:
    return FlareStats(
        total_entries=0,
        errors_last_24h=0,
        warnings_last_24h=0,
        queue_length=0,
        stream_length=0,
    )


def _mask_dsn(dsn: str) -> str:
    """Replace password in the DSN with *** for safe display."""
    try:
        import re
        return re.sub(r"(:)[^:@]+(@)", r"\1***\2", dsn)
    except Exception:
        return dsn


def _empty_request_stats(ring_buffer_size: int) -> "FlareRequestStats":
    return FlareRequestStats(
        total_stored=0,
        ring_buffer_size=ring_buffer_size,
        requests_last_hour=0,
        errors_last_hour=0,
    )


def _row_to_request_entry(row: Any) -> FlareRequestEntry:
    """Convert an asyncpg ``Record`` from the requests table to a :class:`FlareRequestEntry`."""
    headers = row["request_headers"]
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except Exception:
            headers = None

    body = row["request_body"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass

    return FlareRequestEntry(
        id=str(row["id"]),
        timestamp=row["timestamp"],
        method=row["method"],
        path=row["path"],
        status_code=row["status_code"],
        duration_ms=row["duration_ms"],
        request_id=row["request_id"],
        ip_address=row["ip_address"],
        user_agent=row["user_agent"],
        request_headers=headers,
        request_body=body,
        error_id=row["error_id"],
    )


def _row_to_issue(row: Any) -> FlareIssue:
    """Convert an asyncpg ``Record`` from the issues table to a :class:`FlareIssue`."""
    return FlareIssue(
        fingerprint=row["fingerprint"],
        exception_type=row["exception_type"],
        endpoint=row["endpoint"],
        sample_message=row["sample_message"] or "",
        sample_request_id=row["sample_request_id"],
        occurrence_count=row["occurrence_count"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        level=row["level"],
        resolved=row["resolved"],
        resolved_at=row["resolved_at"],
    )


def _row_to_entry(row: Any) -> FlareLogEntry:
    """Convert an asyncpg ``Record`` to a :class:`FlareLogEntry`."""
    ctx = row["context"]
    # asyncpg returns JSONB columns as dicts/lists already when decoded
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = None

    body = row["request_body"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass

    fp = row["issue_fingerprint"] if "issue_fingerprint" in row.keys() else None
    return FlareLogEntry(
        id=str(row["id"]),
        timestamp=row["timestamp"],
        level=row["level"],
        event=row["event"],
        message=row["message"] or "",
        request_id=row["request_id"],
        issue_fingerprint=fp,
        endpoint=row["endpoint"],
        http_method=row["http_method"],
        http_status=row["http_status"],
        ip_address=row["ip_address"],
        duration_ms=row["duration_ms"],
        error=row["error"],
        stack_trace=row["stack_trace"],
        context=ctx,
        request_body=body,
    )
