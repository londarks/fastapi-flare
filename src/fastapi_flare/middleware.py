from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Assigns a UUID4 to every inbound request.

    - Stored at ``request.state.request_id`` for use in exception handlers.
    - Stored at ``request.state.start_time`` (monotonic) for duration_ms computation.
    - Returned as the ``X-Request-ID`` response header so callers can correlate logs.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request.state.request_id = str(uuid.uuid4())
        request.state.start_time = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Records per-endpoint request metrics into ``FlareMetrics`` after every response.

    Reads ``request.state.start_time`` set by ``RequestIdMiddleware`` (which must
    run as the outer middleware, i.e. be added with ``add_middleware`` *after* this
    one) and feeds (endpoint, duration_ms, status_code) into the in-memory store.

    Silently skips recording if ``metrics_instance`` is not yet initialised or
    ``start_time`` is missing on the request state.
    """

    def __init__(self, app, config: "FlareConfig") -> None:
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        metrics = self._config.metrics_instance
        if metrics is None:
            return response

        # Skip internal dashboard routes from metrics
        dashboard_path = getattr(self._config, "dashboard_path", "/flare")
        if request.url.path.startswith(dashboard_path):
            return response

        start = getattr(request.state, "start_time", None)
        if start is not None:
            duration_ms = int((time.monotonic() - start) * 1000)
            # Use route template (/items/{item_id}) instead of concrete path (/items/3321)
            route = request.scope.get("route")
            if route and hasattr(route, "path"):
                endpoint = route.path
            else:
                # No route match (404 or unhandled) — collapse to sentinel so a
                # scanner probing /items/1 … /items/99999 doesn't inflate the dict.
                endpoint = "<unmatched>"
            await metrics.record(endpoint, duration_ms, response.status_code)
        return response
