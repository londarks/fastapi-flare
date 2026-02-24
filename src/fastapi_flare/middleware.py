from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


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
