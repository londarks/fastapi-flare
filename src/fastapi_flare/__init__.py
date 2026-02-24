"""
fastapi-flare
=============

Plug-and-play error tracking and log visualization for FastAPI.
Backed by Redis Streams — no database required.

Minimal usage::

    from fastapi import FastAPI
    from fastapi_flare import setup

    app = FastAPI()
    setup(app, redis_url="redis://localhost:6379")

    # Dashboard available at http://localhost:8000/flare

Full usage::

    from fastapi_flare import setup, FlareConfig

    setup(app, config=FlareConfig(
        redis_url="redis://localhost:6379",
        dashboard_path="/errors",
        dashboard_title="My App — Errors",
        retention_hours=72,
        max_entries=5_000,
    ))
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.exceptions import HTTPException

from fastapi_flare.config import FlareConfig
from fastapi_flare.schema import FlareLogEntry, FlareLogPage, FlareStats

__all__ = ["setup", "FlareConfig", "FlareLogEntry", "FlareLogPage", "FlareStats"]
__version__ = "0.1.0"


def setup(
    app: FastAPI,
    *,
    redis_url: Optional[str] = None,
    config: Optional[FlareConfig] = None,
) -> FlareConfig:
    """
    Wire fastapi-flare into a FastAPI application.

    This function must be called **after** creating the FastAPI instance
    and **before** the application starts serving requests.

    Steps performed:
      1. Build / validate the FlareConfig
      2. Add RequestIdMiddleware (assigns UUID + start_time per request)
      3. Register HTTP exception handler (4xx → WARNING, 5xx → ERROR)
      4. Register generic exception handler (unhandled → ERROR + traceback)
      5. Include the dashboard + API router
      6. Wrap the app lifespan to start/stop the background worker

    :param app:       The FastAPI application instance.
    :param redis_url: Shorthand for ``FlareConfig(redis_url=...)``.
                      Ignored if ``config`` is provided.
    :param config:    Full ``FlareConfig`` instance for advanced configuration.
    :returns:         The resolved ``FlareConfig`` (useful for introspection).
    """
    if config is None:
        config = FlareConfig(redis_url=redis_url) if redis_url else FlareConfig()

    from fastapi_flare.handlers import make_generic_exception_handler, make_http_exception_handler
    from fastapi_flare.middleware import RequestIdMiddleware
    from fastapi_flare.router import make_router
    from fastapi_flare.worker import FlareWorker

    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(HTTPException, make_http_exception_handler(config))
    app.add_exception_handler(Exception, make_generic_exception_handler(config))
    app.include_router(make_router(config))

    worker = FlareWorker(config)
    _wrap_lifespan(app, worker)

    return config


def _wrap_lifespan(app: FastAPI, worker: "FlareWorker") -> None:
    """
    Injects worker start/stop into the app lifespan without overriding
    any lifespan the user may have already defined.

    Worker starts before the user's startup code and stops after the
    user's shutdown code, ensuring clean lifecycle ordering.
    """
    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def flare_lifespan(app: FastAPI):
        worker.start()
        try:
            if existing_lifespan is not None:
                async with existing_lifespan(app):
                    yield
            else:
                yield
        finally:
            await worker.stop()

    app.router.lifespan_context = flare_lifespan
