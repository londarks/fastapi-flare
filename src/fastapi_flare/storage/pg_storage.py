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

from fastapi_flare.schema import FlareLogEntry, FlareStats

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


# ── Schema ────────────────────────────────────────────────────────────────────

def _build_ddl(table: str) -> str:
    """Generate CREATE TABLE + indexes DDL for the given table name."""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id           BIGSERIAL    PRIMARY KEY,
    entry_id     TEXT         NOT NULL DEFAULT '',
    timestamp    TIMESTAMPTZ  NOT NULL,
    level        TEXT         NOT NULL,
    event        TEXT         NOT NULL,
    message      TEXT         NOT NULL DEFAULT '',
    endpoint     TEXT,
    http_method  TEXT,
    http_status  INTEGER,
    duration_ms  INTEGER,
    request_id   TEXT,
    ip_address   TEXT,
    error        TEXT,
    stack_trace  TEXT,
    context      JSONB,
    request_body JSONB
);

CREATE INDEX IF NOT EXISTS idx_{table}_timestamp ON {table} (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_{table}_level     ON {table} (level);
CREATE INDEX IF NOT EXISTS idx_{table}_endpoint  ON {table} (endpoint);
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

    @property
    def _table(self) -> str:
        """Resolved table name — safe: config-controlled, validated on startup."""
        return self._config.pg_table_name

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
                        ip_address, error, stack_trace, context, request_body
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9,
                        $10, $11, $12, $13::jsonb, $14::jsonb
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
        Apply retention policies.  Called every ``worker_interval_seconds``
        by the background worker.

        Steps:
          1. Delete rows older than ``retention_hours``.
          2. Delete the oldest rows exceeding ``max_entries`` (count-based cap).
        """
        try:
            pool = await self._ensure_pool()
            config = self._config
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=config.retention_hours)

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

    return FlareLogEntry(
        id=str(row["id"]),
        timestamp=row["timestamp"],
        level=row["level"],
        event=row["event"],
        message=row["message"] or "",
        request_id=row["request_id"],
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
