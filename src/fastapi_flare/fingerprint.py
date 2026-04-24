"""
Issue fingerprinting for fastapi-flare.
=========================================

Computes a deterministic short hash that groups "the same kind of error"
into a single :class:`~fastapi_flare.schema.FlareIssue`.

Design
------
The fingerprint is intentionally coarser than the raw stack trace so that
an identical bug that fires 500 times produces **one** issue (with
``occurrence_count = 500``), not 500 isolated rows.

Rules, in order of precedence:

1. **Stack trace available** (generic exception path, logging integration):
   hash of ``(exception_type, endpoint, top-5 frames)``. Each frame is
   normalised to ``(basename(file), function_name)`` — absolute paths and
   line numbers are dropped so ordinary refactors do not re-fingerprint the
   same bug.
2. **HTTP-level signal only** (no traceback, e.g. raised ``HTTPException``):
   hash of ``(http, status, endpoint)`` — all 404s on ``/items/{id}`` collapse
   into one issue; a 500 on the same endpoint is a different issue.
3. **Validation error** (``RequestValidationError``): hash of
   ``(validation, endpoint)``.
4. **Fallback**: hash of ``(event, endpoint, message[:200])``.

Not to be confused with the alert-cooldown fingerprint in
:mod:`fastapi_flare.alerting`, which is deliberately coarse (``event+endpoint``)
and used only to throttle repeated Slack/Discord pings.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional


_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line \d+, in (?P<func>\S+)')


def compute_fingerprint(
    *,
    event: Optional[str],
    error: Optional[str],
    stack_trace: Optional[str],
    endpoint: Optional[str],
    http_status: Optional[int],
    message: Optional[str] = None,
) -> str:
    """Return a 16-char hex hash grouping semantically-equivalent errors."""
    ep = endpoint or ""

    if stack_trace:
        exc_type = _extract_exception_type(error) or "Exception"
        frames = _parse_stack_frames(stack_trace, limit=5)
        parts = [exc_type, ep, *[f"{f}:{fn}" for f, fn in frames]]
        return _hash("trace|" + "|".join(parts))

    if http_status is not None and event and event.startswith("http"):
        return _hash(f"http|{http_status}|{ep}")

    if event == "validation_error":
        return _hash(f"validation|{ep}")

    msg = (message or "")[:200]
    return _hash(f"{event or 'unknown'}|{ep}|{msg}")


def _hash(payload: str) -> str:
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()


def _extract_exception_type(error: Optional[str]) -> Optional[str]:
    """Pull ``ValueError`` out of ``"ValueError: bad input"``."""
    if not error:
        return None
    head = error.split(":", 1)[0].strip()
    return head or None


def _parse_stack_frames(stack_trace: str, *, limit: int) -> list[tuple[str, str]]:
    """Extract ``(basename, func_name)`` for the last *limit* frames.

    Line numbers and absolute paths are dropped on purpose so the
    fingerprint survives refactors that don't change the call structure.
    """
    frames: list[tuple[str, str]] = []
    for line in stack_trace.splitlines():
        m = _FRAME_RE.match(line)
        if not m:
            continue
        frames.append((os.path.basename(m.group("file")), m.group("func")))
    return frames[-limit:] if limit > 0 else frames
