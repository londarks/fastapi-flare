"""
FlareWorker: background asyncio task that calls the active storage backend
on a fixed interval to drain pending entries and enforce retention policies.

Lifecycle:
  - start()  schedules an asyncio Task via ensure_future()
  - stop()   cancels the task, awaits clean shutdown, then closes storage

Every worker_interval_seconds the worker:
  1. Calls storage.flush() — the backend handles its own queue / stream
     drain + retention trim logic.
  2. Errors inside flush() are silently swallowed; the loop continues.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


class FlareWorker:
    def __init__(self, config: "FlareConfig") -> None:
        self._config = config
        self._task: Optional[asyncio.Task] = None

    # ── Internals ────────────────────────────────────────────────────────────

    async def _flush(self) -> None:
        """Delegate one flush cycle to the active storage backend."""
        storage = self._config.storage_instance
        if storage is None:
            return
        await storage.flush()

    async def _loop(self) -> None:
        """Main worker loop. Runs until cancelled."""
        while True:
            try:
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Never crash the loop
            await asyncio.sleep(self._config.worker_interval_seconds)

    # ── Public lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Schedule the worker loop as a background asyncio Task."""
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        """
        Cancel the background task, await clean shutdown, then close the
        storage backend (connections, file handles).
        """
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        storage = self._config.storage_instance
        if storage is not None:
            try:
                await storage.close()
            except Exception:
                pass
