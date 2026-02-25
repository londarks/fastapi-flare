from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig

# Scope key where the raw request bytes are cached for exception handlers.
# Exception handlers (HTTPException, RequestValidationError, unhandled 500) all
# receive a **fresh** Request object built from the same scope dict — they do NOT
# share the _body attribute cached by FastAPI/Pydantic on the routing Request.
# Storing the bytes here lets _request_body() recover them even after the ASGI
# receive() callable has been exhausted by the inner app.
_SCOPE_BODY_KEY = "_flare_body"

# Methods that may carry a body worth capturing.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class BodyCacheMiddleware:
    """
    **Pure ASGI middleware** — wraps the ``receive`` callable to intercept
    request-body chunks and stores the complete raw bytes in
    ``scope["_flare_body"]`` **before** FastAPI / Pydantic consume the stream.

    This must be a pure ASGI middleware (not ``BaseHTTPMiddleware``) because
    ``BaseHTTPMiddleware`` creates its own receive channel and we would lose
    access to the original stream before the body is cached.

    Registration order (outermost first after ``add_middleware`` reversal)::

        app.add_middleware(RequestIdMiddleware)
        app.add_middleware(MetricsMiddleware, config=config)
        app.middleware("http")(...)   # handled by setup()

    ``BodyCacheMiddleware`` is registered as the **innermost** ASGI layer so it
    runs after Starlette's ``ExceptionMiddleware`` but before the router.  That
    way the cache is populated on every request before routing occurs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        # Wrap receive: collect chunks transparently, then cache in scope.
        body_chunks: list[bytes] = []
        cached = False

        async def caching_receive() -> dict:
            nonlocal cached
            message = await receive()
            if message.get("type") == "http.request" and not cached:
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    scope[_SCOPE_BODY_KEY] = b"".join(body_chunks)
                    cached = True
            return message

        await self.app(scope, caching_receive, send)



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
