"""
FastAPI router for fastapi-flare.

Registers routes under config.dashboard_path:
  GET /flare               -> Errors dashboard (Jinja2: errors.html)
  GET /flare/metrics       -> Metrics dashboard (Jinja2: metrics.html)
  GET /flare/api/logs      -> Paginated log entries (FlareLogPage)
  GET /flare/api/stats     -> Summary statistics (FlareStats)
  GET /flare/api/metrics   -> In-memory endpoint metrics (FlareMetricsSnapshot)

The router speaks only to the storage protocol and the metrics aggregator.
It has no knowledge of whether Redis, SQLite, or any other backend is in use.
"""
from __future__ import annotations

import pathlib
from typing import Optional

from fastapi import APIRouter, Depends, Query
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from fastapi_flare.schema import (
    FlareEndpointMetric,
    FlareLogPage,
    FlareMetricsSnapshot,
    FlareStats,
)

_TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def make_router(config) -> APIRouter:
    """Returns a configured APIRouter. Called once during setup()."""
    router = APIRouter(prefix=config.dashboard_path, include_in_schema=False)

    deps = []
    if config.dashboard_auth_dependency is not None:
        deps.append(Depends(config.dashboard_auth_dependency))

    _errors_path  = config.dashboard_path
    _metrics_path = config.dashboard_path + "/metrics"
    _api_base     = config.dashboard_path + "/api"

    # -- Dashboard: Errors ----------------------------------------------------

    @router.get("", dependencies=deps)
    async def dashboard(request: Request):
        """Serves the errors dashboard."""
        return _templates.TemplateResponse(
            request=request,
            name="errors.html",
            context={
                "title":        config.dashboard_title,
                "api_base":     _api_base,
                "errors_path":  _errors_path,
                "metrics_path": _metrics_path,
                "active_tab":   "errors",
            },
        )

    # -- Dashboard: Metrics ---------------------------------------------------

    @router.get("/metrics", dependencies=deps)
    async def metrics_dashboard(request: Request):
        """Serves the metrics dashboard (server-side skeleton + JS polling)."""
        return _templates.TemplateResponse(
            request=request,
            name="metrics.html",
            context={
                "title":        config.dashboard_title,
                "api_base":     _api_base,
                "errors_path":  _errors_path,
                "metrics_path": _metrics_path,
                "active_tab":   "metrics",
            },
        )

    # -- REST: logs -----------------------------------------------------------

    @router.get("/api/logs", dependencies=deps)
    async def get_logs(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=500),
        level: Optional[str] = Query(None),
        event: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
    ) -> FlareLogPage:
        storage = config.storage_instance
        if storage is None:
            return FlareLogPage(logs=[], total=0, page=page, limit=limit, pages=0)
        entries, total = await storage.list_logs(
            page=page, limit=limit, level=level, event=event, search=search,
        )
        pages = max(1, (total + limit - 1) // limit)
        return FlareLogPage(logs=entries, total=total, page=page, limit=limit, pages=pages)

    # -- REST: stats ----------------------------------------------------------

    @router.get("/api/stats", dependencies=deps)
    async def get_stats() -> FlareStats:
        storage = config.storage_instance
        if storage is None:
            return FlareStats(
                total_entries=0, errors_last_24h=0,
                warnings_last_24h=0, queue_length=0, stream_length=0,
            )
        return await storage.get_stats()

    # -- REST: metrics --------------------------------------------------------

    @router.get("/api/metrics", dependencies=deps)
    async def get_metrics() -> FlareMetricsSnapshot:
        """Returns the current in-memory metrics snapshot."""
        m = config.metrics_instance
        if m is None:
            return FlareMetricsSnapshot(endpoints=[], total_requests=0, total_errors=0)
        snap = m.snapshot()
        return FlareMetricsSnapshot(
            endpoints=[FlareEndpointMetric(**e) for e in snap],
            total_requests=m.total_requests,
            total_errors=m.total_errors,
            at_capacity=m.at_capacity,
            max_endpoints=m._max_endpoints,
        )

    return router