"""
Redis backend for fastapi-flare.
==================================

Uses two Redis structures:
  - **List** (``config.queue_key``)   — write buffer (LPUSH / RPOP)
  - **Stream** (``config.stream_key``) — durable ordered log (XADD / XREVRANGE)

The write path is fire-and-forget (``enqueue``).
The worker drains the List into the Stream on every ``flush`` cycle.
Retention is enforced by MAXLEN (count-based) + XTRIM MINID (time-based).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi_flare.schema import FlareLogEntry, FlareStats

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


# ── Connection pool ───────────────────────────────────────────────────────────

_connections: dict[str, Any] = {}


def _cache_key(config: "FlareConfig") -> str:
    if config.redis_url:
        return config.redis_url
    return f"{config.redis_host}:{config.redis_port}/{config.redis_db}"


async def _get_client(config: "FlareConfig") -> Any:
    """
    Returns a cached ``redis.asyncio.Redis`` client, or ``None`` if Redis
    is unreachable. Failure is cached so we don't retry on every request.
    """
    key = _cache_key(config)
    if key in _connections:
        return _connections[key]

    try:
        import redis.asyncio as aioredis

        if config.redis_url:
            client = aioredis.from_url(
                config.redis_url,
                password=config.redis_password or None,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
            )
        else:
            client = aioredis.Redis(
                host=config.redis_host,
                port=config.redis_port,
                password=config.redis_password,
                db=config.redis_db,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
            )

        await client.ping()
        _connections[key] = client
    except Exception:
        _connections[key] = None

    return _connections.get(key)


# ── Entry parsing ─────────────────────────────────────────────────────────────

def _entry_id_to_ms(entry_id: str) -> int:
    return int(entry_id.split("-")[0])


def _parse_entry(entry_id: str, fields: dict[str, str]) -> FlareLogEntry:
    ctx = fields.get("context")
    if ctx and isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = None

    body = fields.get("request_body")
    if body and isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass

    ts_raw = fields.get("timestamp")
    if ts_raw:
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            ts = datetime.fromtimestamp(_entry_id_to_ms(entry_id) / 1000, tz=timezone.utc)
    else:
        ts = datetime.fromtimestamp(_entry_id_to_ms(entry_id) / 1000, tz=timezone.utc)

    return FlareLogEntry(
        id=entry_id,
        timestamp=ts,
        level=fields.get("level", "ERROR"),  # type: ignore[arg-type]
        event=fields.get("event", "unknown"),
        message=fields.get("message", ""),
        request_id=fields.get("request_id") or None,
        endpoint=fields.get("endpoint") or None,
        http_method=fields.get("http_method") or None,
        http_status=int(fields["http_status"]) if fields.get("http_status") else None,
        ip_address=fields.get("ip_address") or None,
        duration_ms=int(fields["duration_ms"]) if fields.get("duration_ms") else None,
        error=fields.get("error") or None,
        stack_trace=fields.get("stack_trace") or None,
        context=ctx,
        request_body=body,
    )


# ── RedisStorage ──────────────────────────────────────────────────────────────

class RedisStorage:
    """
    ``FlareStorageProtocol`` implementation backed by Redis Streams.

    The write path is decoupled:

    1. :meth:`enqueue` — LPUSH to a Redis List (fast, non-blocking for callers).
    2. :meth:`flush`  — RPOP batch → XADD to Stream + dead-letter retry + XTRIM.
       Runs every ``worker_interval_seconds`` in the background worker.
    """

    def __init__(self, config: "FlareConfig") -> None:
        self._config = config

    # ── Write path ────────────────────────────────────────────────────────

    async def enqueue(self, entry_dict: dict) -> None:
        """
        LPUSH one JSON-serialized entry to the Redis List buffer.
        Never raises — silently discards on Redis unavailability.
        """
        try:
            client = await _get_client(self._config)
            if client is None:
                return
            await client.lpush(
                self._config.queue_key,
                json.dumps(entry_dict, default=str),
            )
        except Exception:
            pass

    # ── Maintenance ───────────────────────────────────────────────────────

    async def flush(self) -> None:
        """
        Drains the Redis List buffer into the Stream, then applies
        time-based retention (XTRIM MINID).

        On XADD failure, the raw entry is pushed back to the RIGHT of the
        queue so it will be retried on the next flush cycle (dead-letter).
        """
        config = self._config
        client = await _get_client(config)
        if client is None:
            return

        raw_entries: list[str] = []
        try:
            pipe = client.pipeline()
            for _ in range(config.worker_batch_size):
                pipe.rpop(config.queue_key)
            results = await pipe.execute()
            raw_entries = [r for r in results if r is not None]

            if not raw_entries:
                # Nothing to drain; still apply retention trim
                await self._trim(client)
                return

            failed: list[str] = []
            for raw in raw_entries:
                try:
                    entry_dict = json.loads(raw)
                except Exception:
                    continue  # Malformed — discard

                ok = await self._xadd(client, entry_dict)
                if not ok:
                    failed.append(raw)

            # Dead-letter: push failures back for next cycle
            if failed:
                pipe = client.pipeline()
                for raw in failed:
                    pipe.rpush(config.queue_key, raw)
                await pipe.execute()

            await self._trim(client)

        except Exception:
            # Emergency recovery: return raw items to queue
            try:
                if raw_entries:
                    rc = await _get_client(config)
                    if rc:
                        pipe = rc.pipeline()
                        for raw in raw_entries:
                            pipe.rpush(config.queue_key, raw)
                        await pipe.execute()
            except Exception:
                pass

    async def _xadd(self, client: Any, entry_dict: dict) -> bool:
        """XADD one entry to the Stream. Returns False on failure."""
        try:
            flat: dict[str, str] = {}
            for k, v in entry_dict.items():
                if v is None:
                    continue
                flat[k] = (
                    json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
                )
            await client.xadd(
                self._config.stream_key,
                flat,
                maxlen=self._config.max_entries,
                approximate=True,
            )
            return True
        except Exception:
            return False

    async def _trim(self, client: Any) -> None:
        """XTRIM MINID — remove entries older than retention_hours (Redis >=6.2)."""
        try:
            cutoff_ms = int(
                (
                    datetime.now(tz=timezone.utc)
                    - timedelta(hours=self._config.retention_hours)
                ).timestamp()
                * 1000
            )
            await client.xtrim(self._config.stream_key, minid=f"{cutoff_ms}-0")
        except Exception:
            pass

    async def close(self) -> None:
        """Close the cached Redis connection for this config."""
        key = _cache_key(self._config)
        client = _connections.pop(key, None)
        if client:
            try:
                await client.aclose()
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
        XREVRANGE the Stream (newest-first), apply filters in-process,
        and slice to the requested page.

        Fast-path for unfiltered queries: uses XLEN for total count and
        fetches only the entries needed for the requested page, avoiding
        a full stream scan.
        """
        client = await _get_client(self._config)
        if client is None:
            return [], 0

        try:
            # — Fast path: no filters — only fetch what we need
            if not level and not event and not search:
                pipe = client.pipeline()
                pipe.xlen(self._config.stream_key)
                pipe.xrevrange(
                    self._config.stream_key,
                    max="+",
                    min="-",
                    count=page * limit,  # fetch only up to the requested page
                )
                total, raw = await pipe.execute()
                offset = (page - 1) * limit
                entries: list[FlareLogEntry] = []
                for entry_id, fields in raw[offset : offset + limit]:
                    try:
                        entries.append(_parse_entry(entry_id, fields))
                    except Exception:
                        continue
                return entries, total

            # — Filtered path: scan up to max_entries
            raw_all = await client.xrevrange(
                self._config.stream_key,
                max="+",
                min="-",
                count=self._config.max_entries,
            )
        except Exception:
            return [], 0

        all_entries: list[FlareLogEntry] = []
        for entry_id, fields in raw_all:
            try:
                entry = _parse_entry(entry_id, fields)
            except Exception:
                continue

            if level and entry.level != level:
                continue
            if event and event.lower() not in (entry.event or "").lower():
                continue
            if search:
                haystack = f"{entry.message or ''} {entry.error or ''}".lower()
                if search.lower() not in haystack:
                    continue

            all_entries.append(entry)

        total = len(all_entries)
        offset = (page - 1) * limit
        return all_entries[offset : offset + limit], total

    async def get_stats(self) -> FlareStats:
        """
        Compute dashboard summary statistics from the Redis Stream and queue.
        All Redis calls are batched into a single pipeline round-trip.
        """
        config = self._config
        client = await _get_client(config)

        if client is None:
            return FlareStats(
                total_entries=0, errors_last_24h=0, warnings_last_24h=0,
                queue_length=0, stream_length=0,
            )

        cutoff_ms = int(
            (datetime.now(tz=timezone.utc) - timedelta(hours=24)).timestamp() * 1000
        )

        try:
            pipe = client.pipeline()
            pipe.llen(config.queue_key)                               # 0 queue depth
            pipe.xlen(config.stream_key)                              # 1 stream length
            pipe.xrange(config.stream_key, min=f"{cutoff_ms}-0", max="+")  # 2 last 24h
            pipe.xrevrange(config.stream_key, count=1)               # 3 newest entry
            pipe.xrange(config.stream_key, count=1)                  # 4 oldest entry
            queue_length, stream_length, recent, newest_raw, oldest_raw = await pipe.execute()
        except Exception:
            return FlareStats(
                total_entries=0, errors_last_24h=0, warnings_last_24h=0,
                queue_length=0, stream_length=0,
            )

        errors_24h = 0
        warnings_24h = 0
        for _, fields in recent:
            lvl = fields.get("level", "")
            if lvl == "ERROR":
                errors_24h += 1
            elif lvl == "WARNING":
                warnings_24h += 1

        newest_ts: Optional[datetime] = None
        oldest_ts: Optional[datetime] = None
        if newest_raw:
            newest_ts = datetime.fromtimestamp(
                _entry_id_to_ms(newest_raw[0][0]) / 1000, tz=timezone.utc
            )
        if oldest_raw:
            oldest_ts = datetime.fromtimestamp(
                _entry_id_to_ms(oldest_raw[0][0]) / 1000, tz=timezone.utc
            )

        return FlareStats(
            total_entries=stream_length,
            errors_last_24h=errors_24h,
            warnings_last_24h=warnings_24h,
            queue_length=queue_length,
            stream_length=stream_length,
            oldest_entry_ts=oldest_ts,
            newest_entry_ts=newest_ts,
        )
