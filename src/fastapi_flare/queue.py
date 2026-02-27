"""
Log writer for fastapi-flare.
==============================

Single responsibility: build a validated, sanitized log entry dict from raw
handler arguments and hand it off to the active storage backend.

Alert notification scheduling is delegated to
:mod:`fastapi_flare.alerting` — this module has no knowledge of notifiers.

Design invariant: ``push_log()`` MUST NEVER raise an exception.
A logging failure must have zero impact on the user's request path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional


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
    request_body: Optional[Any] = None,
) -> None:
    """
    Build a sanitized log entry dict and delegate to the active storage backend.

    Guards:
      - Returns immediately for unknown log levels.
      - Swallows all exceptions — logging must never impact the request path.
      - Sensitive field values are redacted before storage.

    Alert notifications are scheduled by :func:`fastapi_flare.alerting.schedule_notifications`.
    """
    if level not in ("ERROR", "WARNING"):
        return

    try:
        sensitive = getattr(config, "sensitive_fields", frozenset())

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
            "context": _mask_sensitive(context, sensitive) if context else None,
            "request_body": (
                _mask_sensitive(request_body, sensitive)
                if isinstance(request_body, dict)
                else request_body
            ),
        }

        storage = config.storage_instance
        if storage is not None:
            await storage.enqueue(entry)

        from fastapi_flare.alerting import schedule_notifications
        schedule_notifications(config, level, entry)

    except Exception:  # noqa: BLE001
        pass  # Logging must never impact the user's request
