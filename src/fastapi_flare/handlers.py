"""Exception handlers for fastapi-flare."""
from __future__ import annotations

import json
import time
import traceback
from typing import TYPE_CHECKING, Any, Optional

from fastapi import Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


def _duration_ms(request: Request) -> int | None:
    start = getattr(request.state, "start_time", None)
    if start is None:
        return None
    return int((time.monotonic() - start) * 1000)


def _client_ip(request: Request) -> str | None:
    if request.client:
        return request.client.host
    return None


async def _request_body(request: Request, config: "FlareConfig") -> Optional[Any]:
    """Read and return the decoded request body for non-GET methods.

    - Skipped for GET / HEAD (no body).
    - Capped at ``config.max_request_body_bytes`` (0 = disabled).
    - Tries JSON decode first; falls back to a plain string.
    - Returns ``None`` on any failure or when disabled.
    """
    if config.max_request_body_bytes <= 0:
        return None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    try:
        raw: bytes = await request.body()
        if not raw:
            return None
        raw = raw[: config.max_request_body_bytes]
        try:
            decoded = raw.decode("utf-8", errors="replace")
            return json.loads(decoded)
        except (json.JSONDecodeError, ValueError):
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def make_http_exception_handler(config: "FlareConfig"):
    """
    Handler for FastAPI's HTTPException.
    Logs ALL 4xx and 5xx responses — including 422 raised manually.
    """
    async def handler(request: Request, exc: HTTPException) -> JSONResponse:
        from fastapi_flare.queue import push_log

        if exc.status_code >= 400:
            level = "ERROR" if exc.status_code >= 500 else "WARNING"
            body = await _request_body(request, config)
            await push_log(
                config,
                level=level,
                event="http_exception",
                message=str(exc.detail),
                request_id=getattr(request.state, "request_id", None),
                endpoint=request.url.path,
                http_method=request.method,
                http_status=exc.status_code,
                ip_address=_client_ip(request),
                duration_ms=_duration_ms(request),
                error=f"HTTPException {exc.status_code}: {exc.detail}",
                request_body=body,
            )

        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    return handler


def make_generic_exception_handler(config: "FlareConfig"):
    """
    Handler for all unhandled Python exceptions (500).
    Captures the full traceback and the request body.
    """
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        from fastapi_flare.queue import push_log

        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tb_str = "".join(tb_lines)
        body = await _request_body(request, config)

        await push_log(
            config,
            level="ERROR",
            event="unhandled_exception",
            message=str(exc),
            request_id=getattr(request.state, "request_id", None),
            endpoint=request.url.path,
            http_method=request.method,
            http_status=500,
            ip_address=_client_ip(request),
            duration_ms=_duration_ms(request),
            error=f"{type(exc).__name__}: {str(exc)}",
            stack_trace=tb_str,
            request_body=body,
        )

        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return handler


def make_validation_exception_handler(config: "FlareConfig"):
    """
    Handler for Pydantic RequestValidationError (HTTP 422).

    FastAPI raises this automatically when request data fails schema
    validation.  Logs as WARNING with:
    - A human-readable summary of every violated field.
    - The raw request body that triggered the error.
    """
    async def handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        from fastapi_flare.queue import push_log

        errors = exc.errors()
        # Build a concise human-readable error string
        parts = []
        for e in errors:
            loc = " -> ".join(str(l) for l in e.get("loc", []))
            msg = e.get("msg", "")
            parts.append(f"{loc}: {msg}" if loc else msg)
        error_summary = " | ".join(parts) if parts else "Validation failed"
        first_msg = parts[0] if parts else "Request validation failed"

        # Prefer exc.body (already parsed by FastAPI) over re-reading raw bytes
        body: Any = getattr(exc, "body", None)
        if body is None:
            body = await _request_body(request, config)
        elif isinstance(body, (dict, list)):
            pass  # already decoded
        elif isinstance(body, bytes):
            try:
                body = json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                body = body.decode("utf-8", errors="replace")

        await push_log(
            config,
            level="WARNING",
            event="validation_error",
            message=first_msg,
            request_id=getattr(request.state, "request_id", None),
            endpoint=request.url.path,
            http_method=request.method,
            http_status=422,
            ip_address=_client_ip(request),
            duration_ms=_duration_ms(request),
            error=error_summary,
            request_body=body,
        )

        return JSONResponse(
            status_code=422,
            content={"detail": errors},
        )

    return handler
