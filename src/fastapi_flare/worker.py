"""
FlareWorker: background asyncio task that drains the Redis List buffer
and writes entries to the Redis Stream.

Lifecycle:
  - start() schedules an asyncio Task via ensure_future()
  - stop()  cancels the task and awaits clean shutdown
  - Both are called by the lifespan wrapper in __init__.py

Every worker_interval_seconds the worker:
  1. RPOP up to worker_batch_size items from the List (pipeline for efficiency)
  2. XADD each item to the Stream (via storage.write_entry)
  3. XTRIM the Stream for time-based retention (via storage.trim_by_retention)
  4. On XADD failure, RPUSH items back to the List (dead-letter retry)
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


class FlareWorker:
    def __init__(self, config: "FlareConfig") -> None:
        self._config = config
        self._task: Optional[asyncio.Task] = None

    # ── Internals ────────────────────────────────────────────────────────────

    async def _get_client(self):
        from fastapi_flare.queue import _get_client
        return await _get_client(self._config)

    async def _flush(self) -> None:
        """One flush cycle: drain queue → write stream → trim retention."""
        client = await self._get_client()
        if client is None:
            return

        config = self._config
        raw_entries: list[str] = []

        try:
            # RPOP up to batch_size in one pipeline round-trip
            pipe = client.pipeline()
            for _ in range(config.worker_batch_size):
                pipe.rpop(config.queue_key)
            results = await pipe.execute()
            raw_entries = [r for r in results if r is not None]

            if not raw_entries:
                return

            from fastapi_flare.storage import trim_by_retention, write_entry

            failed: list[str] = []
            for raw in raw_entries:
                try:
                    entry_dict = json.loads(raw)
                except Exception:
                    continue  # Malformed entry — discard silently

                result = await write_entry(client, config, entry_dict)
                if result is None:
                    failed.append(raw)

            # Dead-letter: push failed items back to the RIGHT of the queue
            # (RPOP reads from the right, so RPUSH = retry next cycle)
            if failed:
                pipe = client.pipeline()
                for raw in failed:
                    pipe.rpush(config.queue_key, raw)
                await pipe.execute()

            # Time-based retention trim (no-op if nothing to remove)
            await trim_by_retention(client, config)

        except Exception:
            # Absolute last resort: return raw entries to queue to avoid data loss
            try:
                if raw_entries:
                    recover_client = await self._get_client()
                    if recover_client:
                        pipe = recover_client.pipeline()
                        for raw in raw_entries:
                            pipe.rpush(config.queue_key, raw)
                        await pipe.execute()
            except Exception:
                pass

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
        """Cancel the background task and wait for it to finish cleanly."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
