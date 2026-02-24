"""
Redis List queue used as an incoming buffer for log entries.

Design invariant: push_log() MUST NEVER raise an exception.
A logging failure must have zero impact on the user's request path.
If Redis is unavailable, log entries are silently discarded.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

# Module-level connection cache: { redis_url -> redis.asyncio.Redis | None }
# None means a previous connection attempt failed; we don't retry on every request.
_connections: dict[str, Any] = {}


def _cache_key(config) -> str:
    """Unique key for the connection cache based on Redis coordinates."""
    if config.redis_url:
        return config.redis_url
    return f"{config.redis_host}:{config.redis_port}/{config.redis_db}"


async def _get_client(config) -> Any:
    """
    Returns a cached redis.asyncio client, or None if Redis is unavailable.

    Prefers individual connection fields (host/port/password/db) over a full URL
    so that special characters in passwords are never URL-encoded.
    Falls back to redis_url when explicitly set.
    """
    global _connections
    key = _cache_key(config)

    if key in _connections:
        return _connections[key]

    try:
        import redis.asyncio as aioredis

        if config.redis_url:
            # User provided a full URL — use it directly
            client = aioredis.from_url(
                config.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
            )
        else:
            # Build connection from individual fields (avoids URL-encoding issues)
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


def _mask_sensitive(data: Any, sensitive_fields: frozenset[str]) -> Any:
    """Recursively redacts values whose key contains a sensitive field name."""
    if not isinstance(data, dict):
        return data
    result = {}
    for k, v in data.items():
        key_lower = k.lower()
        if any(s in key_lower for s in sensitive_fields):
            result[k] = "***REDACTED***"
        elif isinstance(v, dict):
            result[k] = _mask_sensitive(v, sensitive_fields)
        elif isinstance(v, list):
            result[k] = [
                _mask_sensitive(i, sensitive_fields) if isinstance(i, dict) else i
                for i in v
            ]
        else:
            result[k] = v
    return result


async def push_log(
    config,
    *,
    level: str,
    event: str,
    message: str,
    request_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    http_method: Optional[str] = None,
    http_status: Optional[int] = None,
    ip_address: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
    stack_trace: Optional[str] = None,
    context: Optional[dict] = None,
) -> None:
    """
    Fire-and-forget: serializes a log entry to JSON and pushes it to the Redis
    List buffer via LPUSH. The worker drains this List into the Stream every
    ``config.worker_interval_seconds`` seconds.

    Never raises. If Redis is unavailable, the entry is silently discarded.
    """
    if level not in ("ERROR", "WARNING"):
        return

    try:
        client = await _get_client(config)
        if client is None:
            return

        masked_context = None
        if context:
            masked_context = _mask_sensitive(context, config.sensitive_fields)

        entry = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": level,
            "event": event,
            "message": message,
            "request_id": request_id,
            "endpoint": endpoint,
            "http_method": http_method,
            "http_status": http_status,
            "ip_address": ip_address,
            "duration_ms": duration_ms,
            "error": error,
            "stack_trace": stack_trace,
            "context": masked_context,
        }

        await client.lpush(config.queue_key, json.dumps(entry, default=str))

    except Exception:
        pass  # Logging must never impact the user's request


async def get_queue_length(config) -> int:
    """Returns the current number of items waiting in the buffer queue."""
    try:
        client = await _get_client(config)
        if client is None:
            return 0
        return await client.llen(config.queue_key)
    except Exception:
        return 0
