"""
fastapi-flare storage — compatibility shim.
=============================================

.. deprecated::
    This module has been superseded by the ``fastapi_flare.storage`` package
    (``storage/__init__.py``, ``storage/base.py``, ``storage/redis_storage.py``,
    ``storage/sqlite_storage.py``).

    The public surface is now:

    .. code-block:: python

        from fastapi_flare.storage import make_storage, FlareStorageProtocol

    Internal callers (worker, router) no longer import from this file —
    they interact exclusively through ``config.storage_instance``.
    This file is kept to avoid breaking any user code that may have
    imported helpers directly.
"""
from fastapi_flare.storage import FlareStorageProtocol, make_storage  # noqa: F401


Stream layout:
  Key:    config.stream_key   (default: "flare:logs")
  Type:   Redis Stream (XADD / XRANGE / XREVRANGE / XTRIM)

Entry ID format: "1708800000000-0"
  - Left part is milliseconds since epoch (auto-assigned by Redis on XADD).
  - IDs are inherently time-ordered — no separate timestamp index needed.
  - XRANGE/XREVRANGE with ID boundaries act as time-range queries.

Pagination:
  - We use XREVRANGE (newest-first) to fetch all entries up to max_entries,
    then apply in-process filter predicates and slice for the requested page.
  - This is efficient for the intended use case (10k max entries, recent errors).

TTL strategy (two complementary mechanisms):
  1. MAXLEN ~ {max_entries}: count-based cap applied on every XADD (O(1) amortized).
  2. XTRIM MINID {cutoff}: time-based eviction called once per worker flush cycle.
     Requires Redis >= 6.2. For older Redis, only count-based cap applies.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi_flare.schema import FlareLogEntry, FlareStats


# ── Conversion helpers ────────────────────────────────────────────────────────


def _entry_id_to_ms(entry_id: str) -> int:
    """Extracts the millisecond timestamp from a Redis Stream entry ID."""
    return int(entry_id.split("-")[0])


def _parse_entry(entry_id: str, fields: dict[str, str]) -> FlareLogEntry:
    """Converts a raw Redis Stream entry into a FlareLogEntry."""
    ctx = fields.get("context")
    if ctx and isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = None

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
    )


# ── Write ─────────────────────────────────────────────────────────────────────


async def write_entry(client: Any, config: Any, entry_dict: dict) -> Optional[str]:
    """
    XADDs one entry to the stream with MAXLEN ~ count cap.
    Returns the Redis Stream entry ID or None on failure.
    """
    try:
        flat: dict[str, str] = {}
        for k, v in entry_dict.items():
            if v is None:
                continue
            flat[k] = json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)

        entry_id = await client.xadd(
            config.stream_key,
            flat,
            maxlen=config.max_entries,
            approximate=True,
        )
        return entry_id
    except Exception:
        return None


# ── Read ──────────────────────────────────────────────────────────────────────


async def read_entries(
    client: Any,
    config: Any,
    *,
    page: int = 1,
    limit: int = 50,
    level: Optional[str] = None,
    event: Optional[str] = None,
    search: Optional[str] = None,
) -> tuple[list[FlareLogEntry], int]:
    """
    Returns (entries_for_page, total_matching) for the given filter parameters.

    Reads the stream in reverse order (newest first) using XREVRANGE,
    applies filters in-process, and slices for the requested page.
    """
    try:
        raw = await client.xrevrange(
            config.stream_key,
            max="+",
            min="-",
            count=config.max_entries,
        )
    except Exception:
        return [], 0

    all_entries: list[FlareLogEntry] = []
    for entry_id, fields in raw:
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
    return all_entries[offset: offset + limit], total


# ── Stats ─────────────────────────────────────────────────────────────────────


async def get_stats(client: Any, config: Any, queue_length: int) -> FlareStats:
    """Computes dashboard summary statistics from the Redis Stream."""
    stream_length = 0
    errors_24h = 0
    warnings_24h = 0
    oldest_ts: Optional[datetime] = None
    newest_ts: Optional[datetime] = None

    try:
        stream_length = await client.xlen(config.stream_key)

        cutoff_ms = int(
            (datetime.now(tz=timezone.utc) - timedelta(hours=24)).timestamp() * 1000
        )
        cutoff_id = f"{cutoff_ms}-0"

        recent = await client.xrange(config.stream_key, min=cutoff_id, max="+")
        for _, fields in recent:
            lvl = fields.get("level", "")
            if lvl == "ERROR":
                errors_24h += 1
            elif lvl == "WARNING":
                warnings_24h += 1

        newest_raw = await client.xrevrange(config.stream_key, count=1)
        if newest_raw:
            newest_ts = datetime.fromtimestamp(
                _entry_id_to_ms(newest_raw[0][0]) / 1000, tz=timezone.utc
            )

        oldest_raw = await client.xrange(config.stream_key, count=1)
        if oldest_raw:
            oldest_ts = datetime.fromtimestamp(
                _entry_id_to_ms(oldest_raw[0][0]) / 1000, tz=timezone.utc
            )
    except Exception:
        pass

    return FlareStats(
        total_entries=stream_length,
        errors_last_24h=errors_24h,
        warnings_last_24h=warnings_24h,
        queue_length=queue_length,
        stream_length=stream_length,
        oldest_entry_ts=oldest_ts,
        newest_entry_ts=newest_ts,
    )


# ── Retention ─────────────────────────────────────────────────────────────────


async def trim_by_retention(client: Any, config: Any) -> None:
    """
    Removes stream entries older than config.retention_hours.
    Uses XTRIM MINID (Redis >= 6.2). No-op on older Redis versions.
    """
    try:
        cutoff_ms = int(
            (datetime.now(tz=timezone.utc) - timedelta(hours=config.retention_hours)).timestamp()
            * 1000
        )
        cutoff_id = f"{cutoff_ms}-0"
        await client.xtrim(config.stream_key, minid=cutoff_id)
    except Exception:
        pass
