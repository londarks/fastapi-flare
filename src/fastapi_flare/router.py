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

import base64
import hashlib
import pathlib
import secrets
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from fastapi_flare.schema import (
    FlareEndpointMetric,
    FlareHealthReport,
    FlareLogPage,
    FlareMetricsSnapshot,
    FlareStats,
    FlareStorageActionResult,
    FlareStorageOverview,
)

_TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def make_router(config) -> APIRouter:
    """Returns a configured APIRouter. Called once during setup()."""
    router = APIRouter(prefix=config.dashboard_path, include_in_schema=False)

    _errors_path  = config.dashboard_path
    _metrics_path = config.dashboard_path + "/metrics"
    _storage_path = config.dashboard_path + "/storage"
    _api_base     = config.dashboard_path + "/api"

    # ── PUBLIC: Health Check (no auth required) ───────────────────────────
    @router.get("/health", response_model=FlareHealthReport, include_in_schema=True)
    async def health_check():
        """
        Returns the operational status of the fastapi-flare subsystems.

        This endpoint is **public** by design — no authentication required —
        so it can be polled by monitoring tools (Uptime Kuma, Kubernetes
        liveness probes, Betterstack, etc.).

        ``status`` values:
          - ``ok``       — storage reachable and worker running.
          - ``degraded`` — worker stopped but storage is ok, or vice-versa.
          - ``down``     — storage is unreachable.
        """
        storage     = config.storage_instance
        worker      = config.worker_instance
        backend     = config.storage_backend  # "redis" | "sqlite"

        worker_running  = worker.is_running  if worker  else False
        flush_cycles    = worker.flush_cycles if worker else 0

        if storage is None:
            return FlareHealthReport(
                status="down",
                storage_backend=backend,
                storage="error",
                storage_error="Storage not initialised",
                worker_running=worker_running,
                worker_flush_cycles=flush_cycles,
                queue_size=0,
            )

        storage_ok, storage_error, queue_size = await storage.health()

        if not storage_ok:
            overall = "down"
        elif not worker_running:
            overall = "degraded"
        else:
            overall = "ok"

        return FlareHealthReport(
            status=overall,
            storage_backend=backend,
            storage="ok" if storage_ok else "error",
            storage_error=storage_error or None,
            worker_running=worker_running,
            worker_flush_cycles=flush_cycles,
            queue_size=queue_size,
        )

    # =========================================================================
    # MODO BROWSER (PKCE) — zitadel_redirect_uri definido
    # =========================================================================
    # Sessão gerenciada pelo SessionMiddleware (cookie assinado "flare_session").
    # PKCE verifier/state ficam em request.session — não em cookies separados.
    # =========================================================================
    if config.zitadel_redirect_uri:
        from datetime import datetime, timedelta
        from fastapi_flare.zitadel import exchange_zitadel_code

        _domain       = config.zitadel_domain
        _client_id    = config.zitadel_client_id
        _project_id   = config.zitadel_project_id
        _redirect_uri = config.zitadel_redirect_uri
        _secure       = _redirect_uri.startswith("https")
        _login_path   = config.dashboard_path + "/auth/login"

        def _session_valid(request: Request) -> bool:
            """True se a sessão contém um usuário autenticado e não expirado."""
            if not request.session.get("authenticated"):
                return False
            expires_str = request.session.get("expires_at")
            if expires_str:
                if datetime.utcnow() > datetime.fromisoformat(expires_str):
                    request.session.clear()
                    return False
            return True

        async def _require_session_api(request: Request) -> None:
            """Dependência para rotas /api — retorna 401 se sem sessão válida."""
            if not _session_valid(request):
                raise HTTPException(
                    status_code=401,
                    detail="Sessão expirada ou ausente. Recarregue o dashboard.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        api_deps = [Depends(_require_session_api)]

        # -- Dashboard: Errors ------------------------------------------------

        _logout_path = config.dashboard_path + "/auth/logout"

        def _base_ctx(active: str, request: Request) -> dict:
            return {
                "title":        config.dashboard_title,
                "api_base":     _api_base,
                "errors_path":  _errors_path,
                "metrics_path": _metrics_path,
                "storage_path": _storage_path,
                "active_tab":   active,
                **_user_context(request),
            }

        def _user_context(request: Request) -> dict:
            u = request.session.get("user") or {}
            return {
                "current_user": {
                    "name":    u.get("name") or u.get("given_name") or "User",
                    "email":   u.get("email", ""),
                    "picture": u.get("picture", ""),
                },
                "logout_path": _logout_path,
            }

        @router.get("")
        async def dashboard(request: Request):
            if not _session_valid(request):
                return RedirectResponse(url=f"{_login_path}?return_to={_errors_path}", status_code=302)
            return _templates.TemplateResponse(request=request, name="errors.html",  context=_base_ctx("errors",  request))

        @router.get("/metrics")
        async def metrics_dashboard(request: Request):
            if not _session_valid(request):
                return RedirectResponse(url=f"{_login_path}?return_to={_metrics_path}", status_code=302)
            return _templates.TemplateResponse(request=request, name="metrics.html", context=_base_ctx("metrics", request))

        @router.get("/storage")
        async def storage_dashboard_auth(request: Request):
            if not _session_valid(request):
                return RedirectResponse(url=f"{_login_path}?return_to={_storage_path}", status_code=302)
            return _templates.TemplateResponse(request=request, name="storage.html", context=_base_ctx("storage", request))

        # -- Auth: Login — inicia fluxo PKCE ----------------------------------

        @router.get("/auth/login")
        async def auth_login(request: Request, return_to: str = _errors_path):
            """Gera PKCE challenge, salva em cookie assinado e redireciona para o Zitadel."""
            # Já autenticado? vai direto pro destino
            if _session_valid(request):
                return RedirectResponse(url=return_to, status_code=302)

            import json as _json
            from itsdangerous import URLSafeTimedSerializer as _Signer

            verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=").decode()
            )
            state = secrets.token_urlsafe(32)

            # Assina o payload PKCE num cookie dedicado — independente do SessionMiddleware
            _secret = config.zitadel_session_secret or secrets.token_hex(32)
            signer  = _Signer(_secret, salt="flare-pkce")
            pkce_token = signer.dumps({"v": verifier, "s": state, "r": return_to})

            params = {
                "client_id":             _client_id,
                "redirect_uri":          _redirect_uri,
                "response_type":         "code",
                "scope":                 "openid profile email",
                "state":                 state,
                "code_challenge":        challenge,
                "code_challenge_method": "S256",
            }
            auth_url = f"https://{_domain}/oauth/v2/authorize?{urlencode(params)}"
            response = RedirectResponse(url=auth_url, status_code=302)
            response.set_cookie(
                "flare_pkce",
                pkce_token,
                httponly=True,
                secure=_secure,
                samesite="lax",
                max_age=600,  # 10 minutos — tempo suficiente para o login
            )
            return response

        # -- Auth: Logout -----------------------------------------------------

        @router.get("/auth/logout")
        async def auth_logout(request: Request):
            """Limpa a sessão e redireciona para a tela de login do Zitadel."""
            request.session.clear()
            return RedirectResponse(url=_login_path, status_code=302)

    # =========================================================================
    # MODO BEARER / SEM AUTH
    # =========================================================================
    else:
        deps = []
        if config.dashboard_auth_dependency is not None:
            deps.append(Depends(config.dashboard_auth_dependency))

        api_deps = deps

        _admin_ctx_base = {
            "api_base":     _api_base,
            "errors_path":  _errors_path,
            "metrics_path": _metrics_path,
            "storage_path": _storage_path,
            "current_user": {"name": "Admin", "email": "", "picture": ""},
            "logout_path":  None,
        }

        def _admin_ctx(active: str) -> dict:
            return {"title": config.dashboard_title, "active_tab": active, **_admin_ctx_base}

        @router.get("", dependencies=deps)
        async def dashboard(request: Request):
            return _templates.TemplateResponse(request=request, name="errors.html",  context=_admin_ctx("errors"))

        @router.get("/metrics", dependencies=deps)
        async def metrics_dashboard(request: Request):
            return _templates.TemplateResponse(request=request, name="metrics.html", context=_admin_ctx("metrics"))

        @router.get("/storage", dependencies=deps)
        async def storage_dashboard(request: Request):
            return _templates.TemplateResponse(request=request, name="storage.html", context=_admin_ctx("storage"))

    # =========================================================================
    # ROTAS /api — compartilhadas por ambos os modos
    # =========================================================================

    @router.get("/api/logs", dependencies=api_deps)
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

    @router.get("/api/stats", dependencies=api_deps)
    async def get_stats() -> FlareStats:
        storage = config.storage_instance
        if storage is None:
            return FlareStats(
                total_entries=0, errors_last_24h=0,
                warnings_last_24h=0, queue_length=0, stream_length=0,
            )
        return await storage.get_stats()

    @router.get("/api/metrics", dependencies=api_deps)
    async def get_metrics() -> FlareMetricsSnapshot:
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

    @router.post("/api/storage/trim", dependencies=api_deps)
    async def storage_trim() -> FlareStorageActionResult:
        """Immediately apply retention policies (time-based + count-based trim)."""
        storage = config.storage_instance
        if storage is None:
            return FlareStorageActionResult(ok=False, action="trim", detail="No storage backend configured")
        try:
            await storage.flush()
            return FlareStorageActionResult(ok=True, action="trim", detail="Retention policies applied")
        except Exception as exc:
            return FlareStorageActionResult(ok=False, action="trim", detail=str(exc))

    @router.post("/api/storage/clear", dependencies=api_deps)
    async def storage_clear() -> FlareStorageActionResult:
        """Permanently delete ALL log entries from storage. Irreversible."""
        storage = config.storage_instance
        if storage is None:
            return FlareStorageActionResult(ok=False, action="clear", detail="No storage backend configured")
        ok, detail = await storage.clear()
        return FlareStorageActionResult(ok=ok, action="clear", detail=detail)

    @router.get("/api/storage/overview", dependencies=api_deps)
    async def storage_overview() -> FlareStorageOverview:
        """Return runtime stats for the active storage backend."""
        storage = config.storage_instance
        if storage is None:
            return FlareStorageOverview(
                backend=config.storage_backend, connected=False,
                error="No storage backend configured",
                max_entries=config.max_entries, retention_hours=config.retention_hours,
            )
        data = await storage.overview()
        return FlareStorageOverview(
            backend=config.storage_backend,
            max_entries=config.max_entries,
            retention_hours=config.retention_hours,
            **data,
        )

    return router


def make_callback_router(config) -> APIRouter:
    """Router sem prefix para o callback OAuth2 — path extraído de zitadel_redirect_uri.

    O state/verifier PKCE são armazenados num cookie assinado dedicado (flare_pkce),
    não no request.session — evita conflito quando a app já tem seu próprio SessionMiddleware.
    """
    from datetime import datetime, timedelta
    from urllib.parse import urlparse
    from itsdangerous import (
        URLSafeTimedSerializer as _Signer,
        BadSignature as _BadSig,
        SignatureExpired as _Expired,
    )
    from fastapi_flare.zitadel import exchange_zitadel_code

    _domain        = config.zitadel_domain
    _client_id     = config.zitadel_client_id
    _redirect_uri  = config.zitadel_redirect_uri
    _errors_path   = config.dashboard_path
    _secure        = _redirect_uri.startswith("https")
    _callback_path = urlparse(_redirect_uri).path
    _secret        = config.zitadel_session_secret or secrets.token_hex(32)

    callback_router = APIRouter(include_in_schema=False)

    @callback_router.get(_callback_path)
    async def zitadel_callback(
        request: Request,
        code: Optional[str] = Query(None),
        state: Optional[str] = Query(None),
        error: Optional[str] = Query(None),
    ):
        """Receives the code from Zitadel, exchanges it for a token, and creates the session."""
        if error:
            raise HTTPException(status_code=400, detail=f"Authentication error: {error}")
        if not code:
            raise HTTPException(status_code=400, detail="Authorization code not provided")

        # Read and verify the dedicated PKCE signed cookie
        pkce_cookie = request.cookies.get("flare_pkce")
        if not pkce_cookie:
            raise HTTPException(
                status_code=400,
                detail="PKCE state cookie missing. Please try logging in again.",
            )

        signer = _Signer(_secret, salt="flare-pkce")
        try:
            pkce_data = signer.loads(pkce_cookie, max_age=600)
        except _Expired:
            raise HTTPException(status_code=400, detail="Login session expired. Please try again.")
        except _BadSig:
            raise HTTPException(status_code=400, detail="Invalid PKCE state — possible CSRF attack.")

        if pkce_data.get("s") != state:
            raise HTTPException(status_code=400, detail="State mismatch — possible CSRF attack.")

        verifier  = pkce_data["v"]
        return_to = pkce_data.get("r", _errors_path)

        access_token = await exchange_zitadel_code(
            domain=_domain,
            client_id=_client_id,
            redirect_uri=_redirect_uri,
            code=code,
            code_verifier=verifier,
        )

        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=15.0) as _hclient:
                ui_resp = await _hclient.get(
                    f"https://{_domain}/oidc/v1/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                ui_resp.raise_for_status()
                userinfo = ui_resp.json()
        except Exception:
            userinfo = {}

        request.session["authenticated"] = True
        request.session["access_token"]  = access_token
        request.session["user"] = {
            "sub":            userinfo.get("sub", ""),
            "email":          userinfo.get("email", ""),
            "name":           userinfo.get("name", userinfo.get("preferred_username", "User")),
            "given_name":     userinfo.get("given_name", ""),
            "family_name":    userinfo.get("family_name", ""),
            "picture":        userinfo.get("picture", ""),
            "email_verified": userinfo.get("email_verified", False),
        }
        expires_at = datetime.utcnow() + timedelta(hours=1)
        request.session["expires_at"] = expires_at.isoformat()

        response = RedirectResponse(url=return_to, status_code=302)
        response.delete_cookie("flare_pkce")
        return response

    return callback_router

