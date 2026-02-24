"""
FastAPI router for fastapi-flare.

Registers three routes under config.dashboard_path:
  GET /flare            → dashboard HTML (self-contained, no external deps)
  GET /flare/api/logs   → paginated log entries (FlareLogPage)
  GET /flare/api/stats  → summary statistics (FlareStats)

The dashboard HTML contains two placeholder tokens replaced at serve time:
  __FLARE_TITLE__     → config.dashboard_title
  __FLARE_API_BASE__  → config.dashboard_path + "/api"

This avoids a Jinja2 runtime dependency for two string substitutions.
"""
from __future__ import annotations

import pathlib
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from fastapi_flare.schema import FlareLogPage, FlareStats

_TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"


def make_router(config) -> APIRouter:
    """Returns a configured APIRouter. Called once during setup()."""
    router = APIRouter(prefix=config.dashboard_path, include_in_schema=False)

    deps = []
    if config.dashboard_auth_dependency is not None:
        deps.append(Depends(config.dashboard_auth_dependency))

    # ── Dashboard HTML ────────────────────────────────────────────────────────

    @router.get("", response_class=HTMLResponse, dependencies=deps)
    async def dashboard() -> HTMLResponse:
        """Serves the self-contained admin dashboard HTML."""
        html = (_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace("__FLARE_TITLE__", config.dashboard_title)
        html = html.replace("__FLARE_API_BASE__", config.dashboard_path + "/api")
        return HTMLResponse(content=html)

    # ── REST: logs ────────────────────────────────────────────────────────────

    @router.get("/api/logs", dependencies=deps)
    async def get_logs(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=500),
        level: Optional[str] = Query(None),
        event: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
    ) -> FlareLogPage:
        from fastapi_flare.queue import _get_client
        from fastapi_flare.storage import read_entries

        client = await _get_client(config)
        if client is None:
            return FlareLogPage(logs=[], total=0, page=page, limit=limit, pages=0)

        entries, total = await read_entries(
            client,
            config,
            page=page,
            limit=limit,
            level=level,
            event=event,
            search=search,
        )
        pages = max(1, (total + limit - 1) // limit)
        return FlareLogPage(logs=entries, total=total, page=page, limit=limit, pages=pages)

    # ── REST: stats ───────────────────────────────────────────────────────────

    @router.get("/api/stats", dependencies=deps)
    async def get_stats() -> FlareStats:
        from fastapi_flare.queue import _get_client, get_queue_length
        from fastapi_flare.storage import get_stats

        client = await _get_client(config)
        queue_len = await get_queue_length(config)

        if client is None:
            return FlareStats(
                total_entries=0,
                errors_last_24h=0,
                warnings_last_24h=0,
                queue_length=queue_len,
                stream_length=0,
            )

        return await get_stats(client, config, queue_len)

    return router
