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

Zitadel authentication (optional)::

    from fastapi_flare import setup, FlareConfig

    setup(app, config=FlareConfig(
        redis_url="redis://localhost:6379",
        zitadel_domain="auth.mycompany.com",
        zitadel_client_id="000000000000000001",
        zitadel_project_id="000000000000000002",
        # /flare dashboard now requires a valid Zitadel Bearer token
    ))

Or bring your own dependency::

    from fastapi_flare.zitadel import make_zitadel_dependency

    dep = make_zitadel_dependency(
        domain="auth.mycompany.com",
        client_id="000000000000000001",
        project_id="000000000000000002",
    )
    setup(app, config=FlareConfig(dashboard_auth_dependency=dep))
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.exceptions import HTTPException

from fastapi_flare.config import FlareConfig
from fastapi_flare.schema import FlareLogEntry, FlareLogPage, FlareStats
from fastapi_flare.zitadel import (
    clear_jwks_cache,
    make_zitadel_dependency,
    verify_zitadel_token,
)

__all__ = [
    "setup",
    "FlareConfig",
    "FlareLogEntry",
    "FlareLogPage",
    "FlareStats",
    # Zitadel auth helpers
    "make_zitadel_dependency",
    "verify_zitadel_token",
    "clear_jwks_cache",
]
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
      2. Auto-wire Zitadel auth dependency (when ``zitadel_domain`` is set)
      3. Add RequestIdMiddleware (assigns UUID + start_time per request)
      4. Register HTTP exception handler (4xx → WARNING, 5xx → ERROR)
      5. Register generic exception handler (unhandled → ERROR + traceback)
      6. Include the dashboard + API router
      7. Wrap the app lifespan to start/stop the background worker

    :param app:       The FastAPI application instance.
    :param redis_url: Shorthand for ``FlareConfig(redis_url=...)``.
                      Ignored if ``config`` is provided.
    :param config:    Full ``FlareConfig`` instance for advanced configuration.
    :returns:         The resolved ``FlareConfig`` (useful for introspection).

    .. note::
        Zitadel auth is activated automatically when **all three** of
        ``zitadel_domain``, ``zitadel_client_id``, and ``zitadel_project_id``
        are configured — either via ``FlareConfig(...)`` or the corresponding
        ``FLARE_ZITADEL_*`` environment variables.  Requires
        ``pip install 'fastapi-flare[auth]'``.
    """
    if config is None:
        config = FlareConfig(redis_url=redis_url) if redis_url else FlareConfig()

    # ── Instantiate storage backend ────────────────────────────────────
    from fastapi_flare.storage import make_storage
    config.storage_instance = make_storage(config)

    # ── Auto-wire Zitadel auth dependency ────────────────────────────────────
    # Activated when the three required Zitadel fields are present AND the user
    # has not already supplied a custom dashboard_auth_dependency.
    if (
        config.zitadel_domain
        and config.zitadel_client_id
        and config.zitadel_project_id
        and config.dashboard_auth_dependency is None
    ):
        extra: list[str] = [
            v
            for v in (
                config.zitadel_old_client_id,
                config.zitadel_old_project_id,
            )
            if v is not None
        ]
        config.dashboard_auth_dependency = make_zitadel_dependency(
            domain=config.zitadel_domain,
            client_id=config.zitadel_client_id,
            project_id=config.zitadel_project_id,
            extra_audiences=extra or None,
        )

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
