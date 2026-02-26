"""
Storage abstraction for fastapi-flare.
======================================

Defines :class:`FlareStorageProtocol` — the single interface that all
storage backends must satisfy.

Rules:
  - The dashboard speaks only to ``FlareStorageProtocol``.
  - Exception handlers speak only to ``FlareStorageProtocol``.
  - The worker speaks only to ``FlareStorageProtocol``.
  - No module outside the ``storage/`` package knows whether Redis,
    SQLite, or any future backend is in use.

Adding a new backend
---------------------
1. Create ``storage/my_backend.py`` implementing all methods below.
2. Register it in ``storage/__init__.py :: make_storage()``.
3. Add the backend name to ``FlareConfig.storage_backend``.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from fastapi_flare.schema import FlareLogEntry, FlareLogPage, FlareStats


@runtime_checkable
class FlareStorageProtocol(Protocol):
    """
    Contract for all fastapi-flare storage backends.

    Every method must be *non-raising* with respect to the request path:
    failures inside :meth:`enqueue` may be silently swallowed so that
    a storage outage never impacts application users.
    All other methods (read-path / maintenance) may raise and the caller
    is responsible for handling errors gracefully.
    """

    # ── Write path (called by exception handlers) ─────────────────────────

    async def enqueue(self, entry_dict: dict) -> None:
        """
        Accept one log entry for durable storage.

        For Redis: pushes to the in-memory List buffer (LPUSH).
        For SQLite: writes directly to the database.

        Must NEVER raise — any failure is swallowed silently.
        """
        ...

    # ── Maintenance (called by the background worker) ─────────────────────

    async def flush(self) -> None:
        """
        Drain any pending in-flight entries and apply retention policies.

        For Redis: RPOP batch from List → XADD to Stream → XTRIM.
        For SQLite: DELETE rows older than ``retention_hours``.

        Called every ``worker_interval_seconds`` by :class:`FlareWorker`.
        """
        ...

    async def close(self) -> None:
        """
        Release all resources held by this backend (connections, file handles).

        Called once during application shutdown by :class:`FlareWorker`.
        """
        ...

    # ── Read path (called by the dashboard router) ────────────────────────

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
        Return a paginated, optionally-filtered list of log entries.

        Returns:
            ``(entries_for_page, total_matching)``
        """
        ...

    async def get_stats(self) -> FlareStats:
        """
        Return summary statistics for the dashboard header cards.

        The returned :class:`FlareStats` includes ``queue_length`` — for
        backends without an explicit queue (e.g. SQLite) this is always 0.
        """
        ...

    async def health(self) -> tuple[bool, str, int]:
        """
        Probe the storage backend to confirm it is reachable and writable.

        Returns:
            ``(ok: bool, error_msg: str | "", queue_size: int)``

        ``ok`` is True when the backend responds correctly.
        ``error_msg`` is empty on success, or a short human-readable message.
        ``queue_size`` is the number of entries currently buffered (0 for SQLite).
        """
        ...

    async def clear(self) -> tuple[bool, str]:
        """
        Permanently delete all stored log entries from this backend.

        For Redis: DEL stream_key + DEL queue_key.
        For SQLite: DELETE FROM logs + VACUUM.

        Returns:
            ``(ok: bool, detail: str)``
        """
        ...

    async def overview(self) -> dict:
        """
        Return a dict of runtime stats for this storage backend.

        The dict is passed directly into ``FlareStorageOverview`` by the router.
        Keys vary by backend — Redis returns stream/queue/memory data;
        SQLite returns file path, size, row count and WAL status.
        Always includes ``connected: bool`` and optionally ``error: str``.
        """
        ...
