"""
Notification scheduler for fastapi-flare.
==========================================

Single responsibility: decide whether to fire notifiers for a captured log
entry, applying level filtering and per-fingerprint cooldown, then schedule
each notifier as a fire-and-forget asyncio background task.

This module is intentionally isolated from storage and HTTP handling.
It knows only about notifier objects, log entry dicts, and cooldown state.

Consumed by :mod:`fastapi_flare.queue` (log writer).
"""
from __future__ import annotations

import asyncio
import time

# Numeric order for level comparison.
# Levels with lower values are less severe.
_LEVEL_ORDER: dict[str, int] = {"WARNING": 0, "ERROR": 1}


def schedule_notifications(config, level: str, entry: dict) -> None:
    """
    Evaluate whether notifiers should fire for *entry* and schedule them.

    Conditions (ALL must be true to fire):
      1. ``config.alert_notifiers`` is non-empty.
      2. *level* is at or above ``config.alert_min_level``.
      3. Cooldown for the ``(event, endpoint)`` fingerprint has expired
         (skipped entirely when ``alert_cooldown_seconds == 0``).

    Each qualifying notifier is scheduled via ``asyncio.ensure_future`` so the
    call returns instantly and never raises.

    Args:
        config: The active :class:`~fastapi_flare.config.FlareConfig` instance.
        level:  Log entry level string — ``"ERROR"`` or ``"WARNING"``.
        entry:  Serialisable dict representing the captured log entry.
    """
    try:
        notifiers = getattr(config, "alert_notifiers", None)
        if not notifiers:
            return

        min_level = getattr(config, "alert_min_level", "ERROR")
        if _LEVEL_ORDER.get(level, 0) < _LEVEL_ORDER.get(min_level, 1):
            return

        cooldown: int = getattr(config, "alert_cooldown_seconds", 300)
        if cooldown > 0:
            cache: dict = config.alert_cache_instance
            fingerprint = f"{entry.get('event', '')}:{entry.get('endpoint', '')}"
            now = time.monotonic()
            if now - cache.get(fingerprint, 0.0) < cooldown:
                return  # still within cooldown window
            cache[fingerprint] = now

        for notifier in notifiers:
            asyncio.ensure_future(notifier.send(entry))

    except Exception:  # noqa: BLE001
        pass  # notification scheduling must never impact request handling
