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
import os
import socket
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


def _generate_worker_id() -> str:
    """Unique identifier for this process across hosts.

    Combines hostname + PID so metrics snapshots from separate pods and
    uvicorn workers don't collide in storage.
    """
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}-{os.getpid()}"


class FlareWorker:
    def __init__(self, config: "FlareConfig") -> None:
        self._config = config
        self._task: Optional[asyncio.Task] = None
        self._flush_cycles: int = 0
        self._started_at: Optional[float] = None
        self._worker_id: str = _generate_worker_id()
        self._last_metrics_flush: float = 0.0
        self._last_request_buffer_flush: float = 0.0

    @property
    def is_running(self) -> bool:
        """True when the background loop task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def flush_cycles(self) -> int:
        """Total number of successful flush() iterations so far."""
        return self._flush_cycles

    @property
    def uptime_seconds(self) -> Optional[int]:
        """Seconds elapsed since the worker was started, or None if not yet started."""
        if self._started_at is None:
            return None
        return int(time.monotonic() - self._started_at)

    # ── Internals ────────────────────────────────────────────────────────────

    @property
    def worker_id(self) -> str:
        """Stable identifier for this process's metrics snapshots."""
        return self._worker_id

    async def _flush(self) -> None:
        """Delegate one flush cycle to the active storage backend."""
        storage = self._config.storage_instance
        if storage is None:
            return
        await storage.flush()

    async def _maybe_flush_request_buffer(self) -> None:
        """Drain the request-tracking in-memory buffer when the interval elapses.

        No-op when ``request_buffer_size`` is 0 (immediate writes) or when there
        is no storage backend. Never raises.
        """
        if int(getattr(self._config, "request_buffer_size", 0) or 0) <= 0:
            return
        interval = int(getattr(self._config, "request_buffer_flush_seconds", 2) or 2)
        now = time.monotonic()
        if now - self._last_request_buffer_flush < interval:
            return

        storage = self._config.storage_instance
        if storage is None:
            return

        try:
            await storage.flush_request_buffer()
        except Exception:
            pass
        finally:
            self._last_request_buffer_flush = now

    async def _maybe_flush_metrics(self) -> None:
        """Persist the in-memory metrics snapshot if the interval has elapsed.

        No-op when ``metrics_persistence`` is disabled or there is no
        storage backend / metrics aggregator. Never raises.
        """
        if not self._config.metrics_persistence:
            return
        interval = self._config.metrics_flush_interval_seconds
        now = time.monotonic()
        if now - self._last_metrics_flush < interval:
            return

        storage = self._config.storage_instance
        metrics = self._config.metrics_instance
        if storage is None or metrics is None:
            return

        try:
            payload = metrics.serialize()
            await storage.flush_metrics(self._worker_id, payload)
        except Exception:
            pass  # never break the worker loop
        finally:
            self._last_metrics_flush = now

    async def _loop(self) -> None:
        """Main worker loop. Runs until cancelled."""
        while True:
            try:
                await self._flush()
                self._flush_cycles += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Never crash the loop
            try:
                await self._maybe_flush_request_buffer()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await self._maybe_flush_metrics()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self._config.worker_interval_seconds)

    # ── Public lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Schedule the worker loop as a background asyncio Task."""
        if self._task is None or self._task.done():
            self._started_at = time.monotonic()
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
