"""
Exception handlers for fastapi-flare.

Uses the closure pattern (make_*) so config is bound at setup() time
without any module-level globals. This makes the library safe for test
suites that create multiple FastAPI instances with different configs.
"""
from __future__ import annotations

import time
import traceback
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.exceptions import HTTPException
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


def make_http_exception_handler(config: "FlareConfig"):
    """
    Returns an async handler for FastAPI's HTTPException.

    - 4xx responses → WARNING level
    - 5xx responses → ERROR level
    - 2xx/3xx → not logged (not an exception)
    """
    async def handler(request: Request, exc: HTTPException) -> JSONResponse:
        from fastapi_flare.queue import push_log

        if exc.status_code >= 400:
            level = "ERROR" if exc.status_code >= 500 else "WARNING"
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
            )

        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    return handler


def make_generic_exception_handler(config: "FlareConfig"):
    """
    Returns an async handler for all unhandled Python exceptions.
    Captures the full traceback and logs it as ERROR level.
    Always returns a generic 500 response so implementation details
    are not exposed to the caller.
    """
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        from fastapi_flare.queue import push_log

        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tb_str = "".join(tb_lines)

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
        )

        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return handler
