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

from fastapi_flare.schema import FlareLogEntry, FlareRequestEntry, FlareRequestStats, FlareStats

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

CREATE INDEX IF NOT EXISTS idx_logs_timestamp   ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_level       ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_endpoint    ON logs(endpoint);
CREATE INDEX IF NOT EXISTS idx_logs_event       ON logs(event);
CREATE INDEX IF NOT EXISTS idx_logs_http_status ON logs(http_status);
-- Composite: covers level filter + time range in the same scan (used by get_stats)
CREATE INDEX IF NOT EXISTS idx_logs_level_ts    ON logs(level, timestamp DESC);
"""


_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS flare_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '{}'
);
"""


_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER  PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,
    method          TEXT     NOT NULL,
    path            TEXT     NOT NULL,
    status_code     INTEGER  NOT NULL,
    duration_ms     INTEGER,
    request_id      TEXT,
    ip_address      TEXT,
    user_agent      TEXT,
    request_headers TEXT,
    request_body    TEXT,
    error_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_timestamp   ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_status_code ON requests(status_code);
CREATE INDEX IF NOT EXISTS idx_requests_method      ON requests(method);
CREATE INDEX IF NOT EXISTS idx_requests_path        ON requests(path);
CREATE INDEX IF NOT EXISTS idx_requests_request_id  ON requests(request_id);
CREATE INDEX IF NOT EXISTS idx_requests_status_ts   ON requests(status_code, timestamp DESC);
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
      - Single-process deployments without a PostgreSQL instance.
      - Lightweight self-hosted setups that don't need horizontal scaling.
    """

    def __init__(self, config: "FlareConfig") -> None:
        self._config = config
        self._db: Any = None
        self._last_retention_at: Optional[datetime] = None  # throttle for flush()  # aiosqlite.Connection, set on first use

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
        await self._db.executescript(_REQUESTS_DDL)
        await self._db.executescript(_SETTINGS_DDL)
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

        Called every ``worker_interval_seconds`` by the background worker, but
        the actual DELETEs run at most once every
        ``retention_check_interval_minutes`` (default 60 min) to avoid
        unnecessary I/O on the SQLite file.
        """
        config = self._config
        interval = config.retention_check_interval_minutes
        now = datetime.now(tz=timezone.utc)

        if interval > 0 and self._last_retention_at is not None:
            elapsed = (now - self._last_retention_at).total_seconds() / 60
            if elapsed < interval:
                return  # not time yet — skip this cycle

        try:
            db = await self._ensure_db()

            # Time-based retention
            cutoff = (now - timedelta(hours=config.retention_hours)).isoformat()
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
            self._last_retention_at = now
        except Exception:
            pass

    async def health(self) -> tuple[bool, str, int]:
        """
        Execute a lightweight SELECT 1 to verify the database is reachable.
        Returns (ok, error_msg, queue_size=0).
        """
        try:
            db = await self._ensure_db()
            await db.execute("SELECT 1")
            return True, "", 0
        except Exception as exc:
            return False, str(exc), 0

    async def clear(self) -> tuple[bool, str]:
        """
        Delete all rows from the logs table and reclaim disk space via VACUUM.
        Returns (ok, detail).
        """
        try:
            db = await self._ensure_db()
            cur = await db.execute("DELETE FROM logs")
            deleted = cur.rowcount
            await db.commit()
            await db.execute("VACUUM")
            return True, f"Deleted {deleted} row(s) and reclaimed disk space"
        except Exception as exc:
            return False, str(exc)

    async def overview(self) -> dict:
        """Return a runtime snapshot dict for the SQLite backend."""
        import os
        config = self._config
        try:
            db = await self._ensure_db()

            cur = await db.execute("SELECT COUNT(*) FROM logs")
            row = await cur.fetchone()
            row_count = row[0] if row else 0

            cur = await db.execute("PRAGMA journal_mode")
            jrow = await cur.fetchone()
            wal_active = (jrow[0].lower() == "wal") if jrow else False

            path = str(config.sqlite_path)
            file_size = os.path.getsize(path) if os.path.exists(path) else 0

            return {
                "connected":       True,
                "db_path":         path,
                "file_size_bytes": file_size,
                "row_count":       row_count,
                "wal_active":      wal_active,
            }
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None
    # ── Request tracking ───────────────────────────────────────────────

    async def enqueue_request(self, entry_dict: dict) -> None:
        """
        INSERT one HTTP request entry and enforce the ring-buffer cap by
        deleting rows beyond ``request_max_entries`` in the same transaction.
        """
        if not self._config.track_requests:
            return
        try:
            db = await self._ensure_db()

            ts_raw = entry_dict.get("timestamp")
            if isinstance(ts_raw, datetime):
                ts = ts_raw.isoformat()
            elif isinstance(ts_raw, str):
                ts = ts_raw
            else:
                ts = datetime.now(tz=timezone.utc).isoformat()

            headers = entry_dict.get("request_headers")
            body = entry_dict.get("request_body")

            async with db.execute("BEGIN"):
                await db.execute(
                    """
                    INSERT INTO requests (
                        timestamp, method, path, status_code, duration_ms,
                        request_id, ip_address, user_agent,
                        request_headers, request_body, error_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        entry_dict.get("method", "GET"),
                        entry_dict.get("path", "/"),
                        entry_dict.get("status_code", 200),
                        entry_dict.get("duration_ms"),
                        entry_dict.get("request_id"),
                        entry_dict.get("ip_address"),
                        entry_dict.get("user_agent"),
                        json.dumps(headers, default=str) if isinstance(headers, (dict, list)) else headers,
                        json.dumps(body, default=str) if isinstance(body, (dict, list)) else body,
                        entry_dict.get("error_id"),
                    ),
                )
                # Ring-buffer enforcement
                await db.execute(
                    """
                    DELETE FROM requests
                    WHERE id NOT IN (
                        SELECT id FROM requests ORDER BY timestamp DESC LIMIT ?
                    )
                    """,
                    (self._config.request_max_entries,),
                )
            await db.commit()
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
            db = await self._ensure_db()
        except Exception:
            return [], 0

        clauses: list[str] = []
        params: list[Any] = []

        if method:
            clauses.append("method = ?")
            params.append(method.upper())
        if status_code is not None:
            clauses.append("status_code = ?")
            params.append(status_code)
        if path:
            clauses.append("path LIKE ?")
            params.append(f"%{path}%")
        if min_duration_ms is not None:
            clauses.append("duration_ms >= ?")
            params.append(min_duration_ms)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        try:
            count_row = await db.execute_fetchall(
                f"SELECT COUNT(*) AS cnt FROM requests {where}", params
            )
            total = count_row[0]["cnt"] if count_row else 0

            offset = (page - 1) * limit
            rows = await db.execute_fetchall(
                f"""
                SELECT * FROM requests {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
            return [_row_to_request_entry(row) for row in rows], total
        except Exception:
            return [], 0

    async def get_request_stats(self) -> FlareRequestStats:
        """Return ring-buffer stats and aggregated metrics."""
        try:
            db = await self._ensure_db()
        except Exception:
            return FlareRequestStats(
                total_stored=0,
                ring_buffer_size=self._config.request_max_entries,
                requests_last_hour=0,
                errors_last_hour=0,
            )

        try:
            cutoff_1h = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
            rows = await db.execute_fetchall(
                """
                SELECT
                    COUNT(*) AS total_stored,
                    SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END)                    AS requests_last_hour,
                    SUM(CASE WHEN status_code >= 400 AND timestamp >= ? THEN 1 ELSE 0 END) AS errors_last_hour,
                    AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END)         AS avg_duration_ms,
                    MAX(duration_ms)                                                   AS slowest_duration_ms
                FROM requests
                """,
                (cutoff_1h, cutoff_1h),
            )
            row = rows[0] if rows else None

            slowest_rows = await db.execute_fetchall(
                """
                SELECT path FROM requests
                WHERE duration_ms = (SELECT MAX(duration_ms) FROM requests)
                LIMIT 1
                """,
            )
            slowest_path = slowest_rows[0]["path"] if slowest_rows else None

            avg = row["avg_duration_ms"] if row and row["avg_duration_ms"] is not None else None
            return FlareRequestStats(
                total_stored=row["total_stored"] or 0 if row else 0,
                ring_buffer_size=self._config.request_max_entries,
                requests_last_hour=row["requests_last_hour"] or 0 if row else 0,
                errors_last_hour=row["errors_last_hour"] or 0 if row else 0,
                avg_duration_ms=int(avg) if avg is not None else None,
                slowest_endpoint=slowest_path,
                slowest_duration_ms=row["slowest_duration_ms"] if row else None,
            )
        except Exception:
            return FlareRequestStats(
                total_stored=0,
                ring_buffer_size=self._config.request_max_entries,
                requests_last_hour=0,
                errors_last_hour=0,
            )

    # ── Settings ───────────────────────────────────────────────────────────

    async def get_settings(self, key: str) -> dict:
        """Return the stored settings dict for *key*, or {} if not found."""
        try:
            db = await self._ensure_db()
            rows = await db.execute_fetchall(
                "SELECT value FROM flare_settings WHERE key = ?", (key,)
            )
            if rows:
                return json.loads(rows[0]["value"])
        except Exception:
            pass
        return {}

    async def save_settings(self, key: str, value: dict) -> None:
        """Upsert *value* JSON under *key* in flare_settings."""
        try:
            db = await self._ensure_db()
            await db.execute(
                "INSERT INTO flare_settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, default=str)),
            )
            await db.commit()
        except Exception:
            pass

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


def _row_to_request_entry(row: Any) -> FlareRequestEntry:
    headers = row["request_headers"] if "request_headers" in row.keys() else None
    if headers and isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except Exception:
            headers = None

    body = row["request_body"] if "request_body" in row.keys() else None
    if body and isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass

    ts = _parse_dt(row["timestamp"]) or datetime.now(tz=timezone.utc)
    error_id = row["error_id"] if "error_id" in row.keys() else None

    return FlareRequestEntry(
        id=str(row["id"]),
        timestamp=ts,
        method=row["method"],
        path=row["path"],
        status_code=row["status_code"],
        duration_ms=row["duration_ms"],
        request_id=row["request_id"],
        ip_address=row["ip_address"],
        user_agent=row["user_agent"],
        request_headers=headers,
        request_body=body,
        error_id=error_id,
    )


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
