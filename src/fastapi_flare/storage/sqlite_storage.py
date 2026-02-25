"""
SQLite backend for fastapi-flare.
====================================

Stores log entries in a local SQLite database using ``aiosqlite`` for
non-blocking async I/O.

Production best-practices applied:
  - WAL journal mode (``PRAGMA journal_mode=WAL``) — allows concurrent
    readers while a writer is active; dramatically reduces lock contention.
  - Indexes on ``timestamp``, ``level``, and ``endpoint`` — keeps filtering
    and pagination fast even at tens of thousands of rows.
  - Lazy init — the database is created and migrated on the first operation,
    so setup() does not need to be async.

Requires::

    pip install "fastapi-flare[sqlite]"
    # or manually: pip install aiosqlite
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi_flare.schema import FlareLogEntry, FlareStats

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


_DDL = """
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT     NOT NULL DEFAULT '',
    timestamp   DATETIME NOT NULL,
    level       TEXT     NOT NULL,
    event       TEXT     NOT NULL,
    message     TEXT     NOT NULL DEFAULT '',
    endpoint    TEXT,
    http_method TEXT,
    http_status INTEGER,
    duration_ms INTEGER,
    request_id  TEXT,
    ip_address  TEXT,
    error       TEXT,
    stack_trace TEXT,
    context     TEXT,
    request_body TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_level     ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_endpoint  ON logs(endpoint);
"""


class SQLiteStorage:
    """
    ``FlareStorageProtocol`` implementation backed by a local SQLite file.

    The write path is fully synchronous with respect to durability: every
    :meth:`enqueue` call immediately persists to the database (via aiosqlite's
    thread-pool executor), so no separate flush step is required.
    :meth:`flush` is used exclusively for time-based retention cleanup.

    Suitable for:
      - Development / local environments.
      - Single-process deployments where Redis is unavailable.
      - Lightweight self-hosted setups that don't need horizontal scaling.
    """

    def __init__(self, config: "FlareConfig") -> None:
        self._config = config
        self._db: Any = None  # aiosqlite.Connection, set on first use

    # ── Lazy init ─────────────────────────────────────────────────────────

    async def _ensure_db(self) -> Any:
        """Open the database and run DDL migrations if not already done."""
        if self._db is not None:
            return self._db

        try:
            import aiosqlite
        except ImportError as exc:
            raise ImportError(
                "aiosqlite is required for the SQLite storage backend. "
                "Install it with: pip install 'fastapi-flare[sqlite]'"
            ) from exc

        self._db = await aiosqlite.connect(self._config.sqlite_path)
        self._db.row_factory = aiosqlite.Row

        await self._db.executescript(_DDL)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.commit()

        # Migration: add request_body column to existing databases
        try:
            await self._db.execute("ALTER TABLE logs ADD COLUMN request_body TEXT")
            await self._db.commit()
        except Exception:
            pass  # column already exists

        return self._db

    # ── Write path ────────────────────────────────────────────────────────

    async def enqueue(self, entry_dict: dict) -> None:
        """
        INSERT one entry directly into the ``logs`` table.
        Never raises — silently discards on any failure.
        """
        try:
            db = await self._ensure_db()

            ctx = entry_dict.get("context")
            body = entry_dict.get("request_body")
            await db.execute(
                """
                INSERT INTO logs (
                    timestamp, level, event, message, endpoint,
                    http_method, http_status, duration_ms, request_id,
                    ip_address, error, stack_trace, context, request_body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_dict.get("timestamp"),
                    entry_dict.get("level", "ERROR"),
                    entry_dict.get("event", "unknown"),
                    entry_dict.get("message", ""),
                    entry_dict.get("endpoint"),
                    entry_dict.get("http_method"),
                    entry_dict.get("http_status"),
                    entry_dict.get("duration_ms"),
                    entry_dict.get("request_id"),
                    entry_dict.get("ip_address"),
                    entry_dict.get("error"),
                    entry_dict.get("stack_trace"),
                    json.dumps(ctx, default=str) if isinstance(ctx, (dict, list)) else ctx,
                    json.dumps(body, default=str) if isinstance(body, (dict, list)) else body,
                ),
            )
            await db.commit()
        except Exception:
            pass

    # ── Maintenance ───────────────────────────────────────────────────────

    async def flush(self) -> None:
        """
        Delete rows older than ``retention_hours`` and enforce ``max_entries``
        by removing the oldest excess rows.

        Called every ``worker_interval_seconds`` by the background worker.
        """
        try:
            db = await self._ensure_db()
            config = self._config

            # Time-based retention
            cutoff = (
                datetime.now(tz=timezone.utc) - timedelta(hours=config.retention_hours)
            ).isoformat()
            await db.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))

            # Count-based cap: keep only the newest max_entries rows
            await db.execute(
                """
                DELETE FROM logs
                WHERE id NOT IN (
                    SELECT id FROM logs ORDER BY timestamp DESC LIMIT ?
                )
                """,
                (config.max_entries,),
            )

            await db.commit()
        except Exception:
            pass

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None

    # ── Read path ─────────────────────────────────────────────────────────

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
        """
        try:
            db = await self._ensure_db()
        except Exception:
            return [], 0

        clauses: list[str] = []
        params: list[Any] = []

        if level:
            clauses.append("level = ?")
            params.append(level)
        if event:
            clauses.append("event LIKE ?")
            params.append(f"%{event}%")
        if search:
            clauses.append("(message LIKE ? OR error LIKE ?)")
            params.append(f"%{search}%")
            params.append(f"%{search}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        try:
            # Total count for pagination
            count_row = await db.execute_fetchall(
                f"SELECT COUNT(*) AS cnt FROM logs {where}", params
            )
            total = count_row[0]["cnt"] if count_row else 0

            offset = (page - 1) * limit
            rows = await db.execute_fetchall(
                f"""
                SELECT * FROM logs {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )

            entries = [_row_to_entry(row) for row in rows]
            return entries, total
        except Exception:
            return [], 0

    async def get_stats(self) -> FlareStats:
        """
        Compute summary stats via COUNT queries.
        ``queue_length`` is always 0 for SQLite (no separate queue).
        """
        try:
            db = await self._ensure_db()
        except Exception:
            return FlareStats(
                total_entries=0,
                errors_last_24h=0,
                warnings_last_24h=0,
                queue_length=0,
                stream_length=0,
            )

        try:
            cutoff_24h = (
                datetime.now(tz=timezone.utc) - timedelta(hours=24)
            ).isoformat()

            rows = await db.execute_fetchall(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN level = 'ERROR'   AND timestamp >= ? THEN 1 ELSE 0 END) AS errors_24h,
                    SUM(CASE WHEN level = 'WARNING' AND timestamp >= ? THEN 1 ELSE 0 END) AS warnings_24h,
                    MIN(timestamp) AS oldest_ts,
                    MAX(timestamp) AS newest_ts
                FROM logs
                """,
                (cutoff_24h, cutoff_24h),
            )

            row = rows[0] if rows else None
            total = row["total"] if row else 0
            errors_24h = row["errors_24h"] or 0 if row else 0
            warnings_24h = row["warnings_24h"] or 0 if row else 0

            oldest_ts = _parse_dt(row["oldest_ts"]) if row and row["oldest_ts"] else None
            newest_ts = _parse_dt(row["newest_ts"]) if row and row["newest_ts"] else None

            return FlareStats(
                total_entries=total,
                errors_last_24h=errors_24h,
                warnings_last_24h=warnings_24h,
                queue_length=0,
                stream_length=total,
                oldest_entry_ts=oldest_ts,
                newest_entry_ts=newest_ts,
            )
        except Exception:
            return FlareStats(
                total_entries=0,
                errors_last_24h=0,
                warnings_last_24h=0,
                queue_length=0,
                stream_length=0,
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _row_to_entry(row: Any) -> FlareLogEntry:
    ctx = row["context"]
    if ctx and isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = None

    body = row["request_body"] if "request_body" in row.keys() else None
    if body and isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass

    ts = _parse_dt(row["timestamp"]) or datetime.now(tz=timezone.utc)

    return FlareLogEntry(
        id=str(row["id"]),
        timestamp=ts,
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
