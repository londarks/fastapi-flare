"""
Zitadel OAuth2 / OIDC JWT authentication for fastapi-flare.
============================================================

Provides a ready-made FastAPI dependency that validates Bearer JWT tokens
signed by a Zitadel identity provider, protecting the /flare dashboard.

Quickstart — automatic wiring via FlareConfig::

    setup(app, config=FlareConfig(
        pg_dsn="postgresql://user:pass@localhost:5432/mydb",
        zitadel_domain="auth.mycompany.com",
        zitadel_client_id="000000000000000001",
        zitadel_project_id="000000000000000002",
    ))

    # /flare now requires a valid Zitadel Bearer token.

Manual wiring (advanced)::

    from fastapi_flare.zitadel import make_zitadel_dependency

    dep = make_zitadel_dependency(
        domain="auth.mycompany.com",
        client_id="000000000000000001",
        project_id="000000000000000002",
    )
    setup(app, config=FlareConfig(dashboard_auth_dependency=dep))

Dependencies required::

    pip install "fastapi-flare[auth]"
    # or manually: pip install httpx python-jose[cryptography]
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import Request

_security = HTTPBearer(
    scheme_name="Bearer Token",
    description="Token JWT obtido do Zitadel OAuth2",
    auto_error=False,
)

# ── Module-level JWKS cache: domain → raw JWKS dict ─────────────────────────
# Populated lazily on first request; cleared via clear_jwks_cache().
# For multi-process deployments, consider using a shared cache
# (e.g. a PostgreSQL table or external store) for JWKS caching.
_jwks_cache: Dict[str, Any] = {}


# ============================================================================
# INTERNAL HELPERS
# ============================================================================

async def _fetch_jwks(domain: str) -> Dict[str, Any]:
    """
    Fetches and caches public keys (JWKS) from Zitadel.

    Args:
        domain: Zitadel domain (e.g. "auth.example.com").

    Returns:
        Parsed JWKS response dict containing the "keys" list.

    Raises:
        HTTPException 503: If the Zitadel JWKS endpoint is unreachable.
        ImportError: If ``httpx`` is not installed.
    """
    if domain in _jwks_cache:
        return _jwks_cache[domain]

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "httpx is required for Zitadel authentication. "
            "Install it with: pip install 'fastapi-flare[auth]'"
        ) from exc

    jwks_url = f"https://{domain}/oauth/v2/keys"

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(jwks_url)
            response.raise_for_status()
            _jwks_cache[domain] = response.json()
            return _jwks_cache[domain]
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Falha ao buscar JWKS do Zitadel ({domain}): {exc}",
            ) from exc


def _extract_rsa_key(jwks: Dict[str, Any], kid: str) -> Dict[str, str]:
    """
    Finds the RSA key matching ``kid`` in the JWKS key set.

    Args:
        jwks: Parsed JWKS dict (contains "keys" list).
        kid: Key ID from the JWT header.

    Returns:
        RSA key dict ready for python-jose, or ``{}`` if not found.
    """
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return {
                "kty": key["kty"],
                "kid": key["kid"],
                "use": key["use"],
                "n":   key["n"],
                "e":   key["e"],
            }
    return {}


# ============================================================================
# TOKEN VERIFICATION
# ============================================================================

async def verify_zitadel_token(
    token: str,
    domain: str,
    valid_audiences: List[str],
) -> Dict[str, Any]:
    """
    Verifies a Zitadel JWT and returns the decoded payload.

    Validation steps:
        1. Decode JWT header to obtain ``kid``
        2. Fetch JWKS from Zitadel (cached)
        3. Find the RSA key matching ``kid`` (retries once on cache miss
           to handle key rotation)
        4. Decode and verify JWT signature + expiry + issuer (RS256)
        5. Manually validate audience against ``valid_audiences``

    Args:
        token:            Raw JWT string.
        domain:           Zitadel domain (e.g. "auth.example.com").
        valid_audiences:  List of accepted ``aud`` values (client ID,
                          project IDs, legacy IDs, etc.).

    Returns:
        Decoded JWT payload (claims dict).

    Raises:
        HTTPException 401: For any validation failure.
        HTTPException 503: If the JWKS endpoint is unreachable.
    """
    try:
        from jose import JWTError, jwt
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "python-jose is required for Zitadel authentication. "
            "Install it with: pip install 'fastapi-flare[auth]'"
        ) from exc

    try:
        # ── 1. Decode JWT header ──────────────────────────────────────────
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido: 'kid' ausente no header JWT",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # ── 2-3. Fetch JWKS and resolve key ──────────────────────────────
        jwks = await _fetch_jwks(domain)
        rsa_key = _extract_rsa_key(jwks, kid)

        if not rsa_key:
            # Possible key rotation — bust cache and retry once
            _jwks_cache.pop(domain, None)
            jwks = await _fetch_jwks(domain)
            rsa_key = _extract_rsa_key(jwks, kid)

        if not rsa_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido: chave pública não encontrada no JWKS",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # ── 4. Verify JWT (signature + exp + iss) ────────────────────────
        expected_issuer = f"https://{domain}"

        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            issuer=expected_issuer,
            options={"verify_aud": False},  # Audience validated manually below
        )

        # ── 5. Validate audience ─────────────────────────────────────────
        token_aud = payload.get("aud", [])
        if isinstance(token_aud, str):
            token_aud = [token_aud]

        if not any(aud in valid_audiences for aud in token_aud):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido: audience não autorizada",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return payload

    except HTTPException:
        raise
    except Exception as exc:
        # Catches JWTError + any unexpected failure
        from jose import JWTError  # type: ignore[import]
        if isinstance(exc, JWTError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token JWT inválido: {exc}",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Erro ao validar token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ============================================================================
# DEPENDENCY FACTORY
# ============================================================================

def make_zitadel_dependency(
    domain: str,
    client_id: str,
    project_id: str,
    extra_audiences: Optional[List[str]] = None,
):
    """
    Returns a FastAPI dependency that validates Zitadel Bearer tokens.

    The returned callable is suitable for use as ``dashboard_auth_dependency``
    in :class:`~fastapi_flare.FlareConfig`.

    When ``zitadel_domain``, ``zitadel_client_id``, and ``zitadel_project_id``
    are set in :class:`~fastapi_flare.FlareConfig`, this dependency is wired
    **automatically** inside :func:`~fastapi_flare.setup` — no manual call
    is needed.

    Usage (automatic — recommended)::

        config = FlareConfig(
            zitadel_domain="auth.mycompany.com",
            zitadel_client_id="000000000000000001",
            zitadel_project_id="000000000000000002",
        )
        setup(app, config=config)

    Usage (manual)::

        from fastapi_flare.zitadel import make_zitadel_dependency

        dep = make_zitadel_dependency(
            domain="auth.mycompany.com",
            client_id="000000000000000001",
            project_id="000000000000000002",
            extra_audiences=["000000000000000003"],  # legacy client id (migration)
        )
        config = FlareConfig(dashboard_auth_dependency=dep)
        setup(app, config=config)

    Args:
        domain:           Zitadel domain (e.g. "auth.example.com").
        client_id:        OAuth2 Client ID of the application.
        project_id:       Zitadel Project ID.
        extra_audiences:  Additional accepted ``aud`` values (e.g. legacy client
                          IDs that must continue to work during migrations).

    Returns:
        Async FastAPI dependency function.
    """
    valid_audiences: List[str] = list({client_id, project_id, *(extra_audiences or [])})

    async def _zitadel_auth(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ) -> Dict[str, Any]:
        """Validates the Bearer token issued by Zitadel."""
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token de autenticação não fornecido",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await verify_zitadel_token(
            credentials.credentials,
            domain,
            valid_audiences,
        )

    return _zitadel_auth


# ============================================================================
# BROWSER-BASED OAUTH2 PKCE FLOW
# ============================================================================

class ZitadelBrowserRedirect(Exception):
    """
    Raised by :func:`make_zitadel_browser_dependency` when the user has no
    valid session cookie.  Caught by the exception handler registered in
    ``setup()`` to issue a 302 redirect to Zitadel's authorization endpoint.

    Attributes:
        location: Full Zitadel authorization URL (with PKCE challenge).
        verifier: PKCE ``code_verifier`` — stored in a short-lived cookie.
        state:    CSRF state token — stored in a short-lived cookie.
    """

    def __init__(self, location: str, verifier: str, state: str) -> None:
        self.location = location
        self.verifier = verifier
        self.state = state


def make_zitadel_browser_dependency(
    domain: str,
    client_id: str,
    project_id: str,
    redirect_uri: str,
    extra_audiences: Optional[List[str]] = None,
):
    """
    Returns a FastAPI dependency for **browser-based** OAuth2 PKCE via Zitadel.

    When a user opens the /flare dashboard without a valid ``flare_token``
    cookie they are transparently redirected to Zitadel's login page.  After
    a successful login Zitadel calls ``redirect_uri`` (``/flare/callback``),
    which exchanges the code for a token, sets the cookie, and redirects back
    to the dashboard.

    Usage (automatic — configure ``zitadel_redirect_uri`` in FlareConfig)::

        setup(app, config=FlareConfig(
            zitadel_domain="auth.mycompany.com",
            zitadel_client_id="000000000000000001",
            zitadel_project_id="000000000000000002",
            zitadel_redirect_uri="https://myapp.com/flare/callback",
        ))

    Args:
        domain:           Zitadel domain (e.g. ``auth.example.com``).
        client_id:        OAuth2 Client ID.
        project_id:       Zitadel Project ID.
        redirect_uri:     Callback URL registered in Zitadel
                          (must match exactly, e.g. ``https://myapp.com/flare/callback``).
        extra_audiences:  Additional accepted ``aud`` values (legacy migration).

    Returns:
        Async FastAPI dependency that either returns the decoded JWT payload
        or raises :class:`ZitadelBrowserRedirect`.
    """
    valid_audiences: List[str] = list({client_id, project_id, *(extra_audiences or [])})

    async def _browser_auth(request: Request) -> Dict[str, Any]:
        """Validates the session cookie; redirects to Zitadel if absent/expired."""
        token = request.cookies.get("flare_token")

        if token:
            try:
                return await verify_zitadel_token(token, domain, valid_audiences)
            except HTTPException:
                pass  # Token expirado ou inválido — cai no redirect abaixo

        # Gera PKCE challenge
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(43)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"https://{domain}/oauth/v2/authorize?{urlencode(params)}"
        raise ZitadelBrowserRedirect(auth_url, verifier, state)

    return _browser_auth


async def exchange_zitadel_code(
    domain: str,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> Dict[str, Any]:
    """
    Exchanges an OAuth2 authorization code for a Zitadel access token.

    Called by the ``/flare/callback`` route after Zitadel redirects back
    with ``?code=...&state=...``.

    Args:
        domain:        Zitadel domain.
        client_id:     OAuth2 Client ID (used in ``client_id`` form field).
        redirect_uri:  Must exactly match the URI used in the authorization request.
        code:          Authorization code received from Zitadel.
        code_verifier: PKCE verifier stored in the short-lived cookie.

    Returns:
        Full token response dict (contains ``access_token``, and optionally
        ``refresh_token``, ``expires_in``, ``id_token``, etc.).

    Raises:
        HTTPException 502: If the token exchange request fails.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "httpx is required for Zitadel authentication. "
            "Install it with: pip install 'fastapi-flare[auth]'"
        ) from exc

    token_url = f"https://{domain}/oauth/v2/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(token_url, data=data)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao trocar código por token no Zitadel: {exc}",
            ) from exc

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Zitadel não retornou access_token na resposta do token endpoint",
        )
    return payload  # dict contendo access_token, e opcionalmente refresh_token, expires_in


# ============================================================================
# UTILITIES
# ============================================================================

def clear_jwks_cache(domain: Optional[str] = None) -> None:
    """
    Clears the in-memory JWKS cache.

    Useful when rotating keys or in tests to avoid stale public-key lookups.

    Args:
        domain: If provided, clears only the cache entry for that domain.
                If ``None``, clears **all** cached JWKS entries.
    """
    if domain is not None:
        _jwks_cache.pop(domain, None)
    else:
        _jwks_cache.clear()


async def refresh_zitadel_token(
    domain: str,
    client_id: str,
    refresh_token: str,
) -> Dict[str, Any]:
    """
    Uses a Zitadel refresh_token to obtain a new access_token (and possibly
    a new refresh_token).

    The ``offline_access`` scope must have been requested during the original
    PKCE authorization to receive a refresh token.

    Args:
        domain:        Zitadel domain (e.g. ``auth.example.com``).
        client_id:     OAuth2 Client ID.
        refresh_token: The refresh token stored in the user session.

    Returns:
        Dict with at least ``access_token`` and optionally ``refresh_token``
        and ``expires_in`` (integer seconds).

    Raises:
        HTTPException 502: If the token-refresh request fails or Zitadel
                           returns an error (e.g. session revoked).
        ImportError:       If ``httpx`` is not installed.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "httpx is required for Zitadel authentication. "
            "Install it with: pip install 'fastapi-flare[auth]'"
        ) from exc

    token_url = f"https://{domain}/oauth/v2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.post(token_url, data=data)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao renovar token no Zitadel ({domain}): {exc}",
            ) from exc

    payload = response.json()
    if "error" in payload:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Zitadel recusou o refresh_token: {payload.get('error_description', payload['error'])}",
        )

    access_token = payload.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Zitadel não retornou access_token na resposta de refresh",
        )

    return payload
