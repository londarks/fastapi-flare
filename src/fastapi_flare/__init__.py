"""
fastapi-flare
=============

Plug-and-play error tracking and debugger/metrics dashboard for FastAPI.
Zero-config by default (SQLite), PostgreSQL-ready for production.

Quick start — zero config, works immediately::

    from fastapi import FastAPI
    from fastapi_flare import setup

    app = FastAPI()
    setup(app)
    # Dashboard at http://localhost:8000/flare
    # Uses SQLite (flare.db) by default — no setup required.

SQLite (explicit / custom path)::

    from fastapi_flare import setup, FlareConfig

    setup(app, config=FlareConfig(
        storage_backend="sqlite",
        sqlite_path="/data/flare.db",
        dashboard_path="/errors",
        dashboard_title="My App — Errors",
        retention_hours=72,
        max_entries=5_000,
    ))

PostgreSQL (production)::

    setup(app, config=FlareConfig(
        storage_backend="postgresql",
        pg_dsn="postgresql://user:pass@localhost:5432/mydb",
    ))

Zitadel authentication (optional)::

    from fastapi_flare import setup, FlareConfig

    setup(app, config=FlareConfig(
        pg_dsn="postgresql://...",
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
from fastapi_flare.metrics import FlareMetrics
from fastapi_flare.notifiers import DiscordNotifier, SlackNotifier, TeamsNotifier, WebhookNotifier
from fastapi_flare.schema import FlareLogEntry, FlareLogPage, FlareMetricsSnapshot, FlareStats
from fastapi_flare.zitadel import (
    ZitadelBrowserRedirect,
    clear_jwks_cache,
    exchange_zitadel_code,
    make_zitadel_browser_dependency,
    make_zitadel_dependency,
    verify_zitadel_token,
)

__all__ = [
    "setup",
    "FlareConfig",
    "FlareMetrics",
    "FlareLogEntry",
    "FlareLogPage",
    "FlareStats",
    "FlareMetricsSnapshot",
    # Webhook notifiers
    "WebhookNotifier",
    "SlackNotifier",
    "DiscordNotifier",
    "TeamsNotifier",
    # Zitadel auth helpers
    "make_zitadel_dependency",
    "make_zitadel_browser_dependency",
    "exchange_zitadel_code",
    "verify_zitadel_token",
    "clear_jwks_cache",
    "ZitadelBrowserRedirect",
]
__version__ = "0.1.4"


def setup(
    app: FastAPI,
    *,
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

    :param app:    The FastAPI application instance.
    :param config: Full ``FlareConfig`` instance.  When omitted, config is
                   read from environment variables (``FLARE_*`` prefix) or
                   ``.env`` file.
    :returns:      The resolved ``FlareConfig`` (useful for introspection).

    .. note::
        Zitadel auth is activated automatically when **all three** of
        ``zitadel_domain``, ``zitadel_client_id``, and ``zitadel_project_id``
        are configured — either via ``FlareConfig(...)`` or the corresponding
        ``FLARE_ZITADEL_*`` environment variables.  Requires
        ``pip install 'fastapi-flare[auth]'``.
    """
    if config is None:
        config = FlareConfig()

    # ── Instantiate storage backend ────────────────────────────────────
    from fastapi_flare.storage import make_storage
    config.storage_instance = make_storage(config)
    # ── Instantiate in-memory metrics aggregator ──────────────────────────
    config.metrics_instance = FlareMetrics(max_endpoints=config.metrics_max_endpoints)
    # ── Auto-wire Zitadel auth dependency (modo Bearer) ────────────────────────
    # Ativado quando os três campos Zitadel estão presentes E o usuário não
    # forneceu dashboard_auth_dependency customizado.
    # Modo browser (zitadel_redirect_uri definido) é gerenciado dentro do
    # router.py diretamente — não usa dashboard_auth_dependency.
    if (
        config.zitadel_domain
        and config.zitadel_client_id
        and config.zitadel_project_id
        and not config.zitadel_redirect_uri  # browser mode é feito no router.py
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

    from fastapi_flare.handlers import (
        make_generic_exception_handler,
        make_http_exception_handler,
        make_validation_exception_handler,
    )
    from fastapi.exceptions import RequestValidationError
    from fastapi_flare.middleware import BodyCacheMiddleware, MetricsMiddleware, RequestIdMiddleware
    from fastapi_flare.router import make_router, make_callback_router
    from fastapi_flare.worker import FlareWorker

    # Middleware stack — add_middleware() inserts in reverse, so the LAST call
    # becomes the outermost layer (first to see the request):
    #
    #   RequestIdMiddleware   → outermost: sets request_id + start_time
    #   MetricsMiddleware     → middle:    records latency/status after response
    #   BodyCacheMiddleware   → innermost: wraps receive() to store raw body bytes
    #                           in scope["_flare_body"] BEFORE FastAPI/Pydantic
    #                           consumes the stream.  Exception handlers receive a
    #                           *fresh* Request object and can't rely on request._body
    #                           being set; the scope dict is shared, so they read
    #                           the cached bytes from there instead.
    app.add_middleware(BodyCacheMiddleware)
    app.add_middleware(MetricsMiddleware, config=config)
    app.add_middleware(RequestIdMiddleware)

    # SessionMiddleware — necessário para o fluxo PKCE browser.
    # Deve ser adicionado por último (torna-se a camada mais externa),
    # garantindo que request.session esteja disponível para todos os handlers.
    if config.zitadel_redirect_uri:
        import secrets as _secrets
        from starlette.middleware.sessions import SessionMiddleware as _SessionMiddleware

        _session_secret = config.zitadel_session_secret or _secrets.token_hex(32)
        _secure = config.zitadel_redirect_uri.startswith("https")
        app.add_middleware(
            _SessionMiddleware,
            secret_key=_session_secret,
            session_cookie="flare_session",
            max_age=3600 * 24,  # 24 horas
            same_site="lax",
            https_only=_secure,
        )

    app.add_exception_handler(HTTPException, make_http_exception_handler(config))
    app.add_exception_handler(RequestValidationError, make_validation_exception_handler(config))
    app.add_exception_handler(Exception, make_generic_exception_handler(config))
    app.include_router(make_router(config))
    if config.zitadel_redirect_uri:
        app.include_router(make_callback_router(config))

    worker = FlareWorker(config)
    config.worker_instance = worker
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
