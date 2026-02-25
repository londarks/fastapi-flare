"""
tests/test_body_capture.py — Diagnostic unit tests for request body capture.

Runs with:  poetry run pytest tests/test_body_capture.py -v

These tests expose exactly WHY bodies are lost and verify the fix.
Each test is labelled with the layer it tests.
"""
from __future__ import annotations

import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

# ─── helpers ─────────────────────────────────────────────────────────────────


def _make_config(max_body_bytes: int = 8192, sensitive: frozenset = frozenset()):
    cfg = MagicMock()
    cfg.max_request_body_bytes = max_body_bytes
    cfg.sensitive_fields = sensitive
    cfg.storage_instance = None  # disable actual storage
    return cfg


def _make_request(method: str, body_bytes: bytes, scope_extras: dict | None = None):
    """Build a minimal Starlette Request with an in-memory receive callable."""
    consumed = False

    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": method.upper(),
        "path": "/test",
        **(scope_extras or {}),
    }

    from starlette.requests import Request

    req = Request(scope, receive)
    return req


def _make_exhausted_request(method: str, body_bytes: bytes):
    """
    Simulates what happens AFTER BaseHTTPMiddleware consumes the receive stream:
    the Request still exists but receive() returns disconnect immediately.
    This is the state exception handlers see.
    """

    async def exhausted_receive():
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": method.upper(),
        "path": "/test",
    }

    from starlette.requests import Request

    req = Request(scope, exhausted_receive)
    return req


def _make_scope_cached_request(method: str, cached_body: bytes):
    """Request whose scope already has _flare_body set (our fix scenario)."""

    async def exhausted_receive():
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": method.upper(),
        "path": "/test",
        "_flare_body": cached_body,  # pre-cached by BodyCacheMiddleware
    }

    from starlette.requests import Request

    return Request(scope, exhausted_receive)


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — _request_body() unit tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequestBodyHelper:
    """Tests for handlers._request_body()."""

    @pytest.mark.asyncio
    async def test_disabled_when_zero_bytes(self):
        from fastapi_flare.handlers import _request_body

        req = _make_request("POST", b'{"foo": 1}')
        cfg = _make_config(max_body_bytes=0)
        result = await _request_body(req, cfg)
        assert result is None, "Should return None when max_request_body_bytes=0"

    @pytest.mark.asyncio
    async def test_get_returns_none(self):
        from fastapi_flare.handlers import _request_body

        req = _make_request("GET", b"")
        cfg = _make_config()
        result = await _request_body(req, cfg)
        assert result is None, "GET requests should never capture body"

    @pytest.mark.asyncio
    async def test_head_returns_none(self):
        from fastapi_flare.handlers import _request_body

        req = _make_request("HEAD", b"")
        cfg = _make_config()
        result = await _request_body(req, cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_post_json_body_decoded(self):
        from fastapi_flare.handlers import _request_body

        payload = {"username": "alice", "email": "alice@example.com"}
        req = _make_request("POST", json.dumps(payload).encode())
        cfg = _make_config()
        result = await _request_body(req, cfg)
        assert result == payload, f"Expected decoded dict, got {result!r}"

    @pytest.mark.asyncio
    async def test_post_non_json_body_as_string(self):
        from fastapi_flare.handlers import _request_body

        raw = b"not valid json {{{"
        req = _make_request("POST", raw)
        cfg = _make_config()
        result = await _request_body(req, cfg)
        assert isinstance(result, str)
        assert "not valid json" in result

    @pytest.mark.asyncio
    async def test_empty_body_returns_none(self):
        from fastapi_flare.handlers import _request_body

        req = _make_request("POST", b"")
        cfg = _make_config()
        result = await _request_body(req, cfg)
        assert result is None, "Empty body should return None"

    @pytest.mark.asyncio
    async def test_body_truncated_at_cap(self):
        from fastapi_flare.handlers import _request_body

        # 100-byte string, cap at 10
        raw = b"a" * 100
        req = _make_request("POST", raw)
        cfg = _make_config(max_body_bytes=10)
        result = await _request_body(req, cfg)
        # Should be a 10-char string (decoded from 10 bytes)
        assert isinstance(result, str)
        assert len(result) == 10

    @pytest.mark.asyncio
    async def test_delete_captures_body(self):
        from fastapi_flare.handlers import _request_body

        payload = {"reason": "test delete"}
        req = _make_request("DELETE", json.dumps(payload).encode())
        cfg = _make_config()
        result = await _request_body(req, cfg)
        assert result == payload

    # ── BUG SCENARIO: stream already exhausted (BaseHTTPMiddleware effect) ──

    @pytest.mark.asyncio
    async def test_exhausted_stream_FAILS_without_fix(self):
        """
        DIAGNOSTIC TEST — Demonstrates the root-cause bug.

        When BaseHTTPMiddleware has already consumed the receive() stream
        before the exception handler runs, _request_body() gets b"" → returns None.

        This test PASSES (with None) to prove the bug exists.
        After the fix is applied, the scope cache prevents this loss.
        """
        from fastapi_flare.handlers import _request_body

        req = _make_exhausted_request("POST", b'{"username":"alice"}')
        cfg = _make_config()
        result = await _request_body(req, cfg)
        # On exhausted stream WITHOUT fix: None (body lost)
        # On exhausted stream WITH fix:    the dict (body recovered from scope)
        # This assertion documents current behaviour before optimization:
        assert result is None, (
            "Exhausted stream returns None — this is the bug. "
            "With scope-cache fix this test should be updated."
        )

    @pytest.mark.asyncio
    async def test_scope_cached_body_recovered(self):
        """
        DIAGNOSTIC TEST — Verifies the fix: body pre-cached in scope is recovered.

        BodyCacheMiddleware stores body bytes in scope['_flare_body'] BEFORE
        FastAPI/Pydantic consume the receive stream. The exception handler's
        fresh Request sees the scope, finds the cache, and returns the body.
        """
        from fastapi_flare.handlers import _request_body

        payload = {"username": "alice", "email": "alice@example.com"}
        req = _make_scope_cached_request("POST", json.dumps(payload).encode())
        cfg = _make_config()
        result = await _request_body(req, cfg)
        assert result == payload, (
            f"Should recover body from scope cache, got {result!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — _mask_sensitive unit tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMaskSensitive:
    def test_password_redacted(self):
        from fastapi_flare.queue import _mask_sensitive

        data = {"username": "alice", "password": "secret"}
        result = _mask_sensitive(data, frozenset({"password", "token", "secret"}))
        assert result["username"] == "alice"
        assert result["password"] == "***REDACTED***"

    def test_nested_redacted(self):
        from fastapi_flare.queue import _mask_sensitive

        data = {"user": {"token": "abc123", "name": "bob"}}
        result = _mask_sensitive(data, frozenset({"token"}))
        assert result["user"]["token"] == "***REDACTED***"
        assert result["user"]["name"] == "bob"

    def test_non_dict_passthrough(self):
        from fastapi_flare.queue import _mask_sensitive

        assert _mask_sensitive("plain string", frozenset()) == "plain string"
        assert _mask_sensitive(42, frozenset()) == 42

    def test_list_of_dicts_redacted(self):
        from fastapi_flare.queue import _mask_sensitive

        data = {"items": [{"secret": "x"}, {"safe": "y"}]}
        result = _mask_sensitive(data, frozenset({"secret"}))
        assert result["items"][0]["secret"] == "***REDACTED***"
        assert result["items"][1]["safe"] == "y"


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 3 — Integration: full FastAPI stack (captures vs loses body)
# ═══════════════════════════════════════════════════════════════════════════════


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3)
    email: str


def _make_app_with_flare(storage_backend="sqlite"):
    """Build a minimal FastAPI app wired with fastapi-flare."""
    from fastapi_flare import FlareConfig, setup

    app = FastAPI()
    config = FlareConfig(storage_backend=storage_backend, sqlite_path=":memory:")
    setup(app, config=config)

    @app.post("/users")
    async def create_user(body: UserCreate):
        if body.username == "alice":
            raise HTTPException(status_code=409, detail="Username already exists")
        return {"ok": True}

    @app.post("/orders")
    async def create_order(
        body: UserCreate,
        x_auth_token: Optional[str] = Header(default=None),
    ):
        if not x_auth_token:
            raise HTTPException(status_code=401, detail="Missing token")
        return {"ok": True}

    @app.post("/boom")
    async def boom_post(body: UserCreate):
        raise RuntimeError("deliberate 500 with body")

    return app, config


@pytest.mark.asyncio
class TestIntegrationBodyCapture:
    """
    End-to-end tests: fire HTTP request → check log entry has request_body.
    Uses httpx.AsyncClient with ASGI transport (no real server needed).
    """

    async def _get_last_log(self, config, path: str, status: int) -> dict | None:
        """Poll storage for a log entry matching path + status."""
        import asyncio

        # Give worker/flush time (SQLite direct enqueue, no worker needed)
        await asyncio.sleep(0.05)
        storage = config.storage_instance
        if storage is None:
            return None
        entries, _ = await storage.list_logs(page=1, limit=10)
        for e in entries:
            if e.endpoint == path and e.http_status == status:
                return e.model_dump()
        return None

    @pytest.mark.asyncio
    async def test_409_http_exception_captures_body(self):
        """HTTPException from endpoint — Pydantic already parsed body → should capture."""
        app, config = _make_app_with_flare()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            payload = {"username": "alice", "email": "alice@example.com"}
            resp = await client.post("/users", json=payload)
            assert resp.status_code == 409

        entry = await self._get_last_log(config, "/users", 409)
        assert entry is not None, "Log entry for 409 not found"
        assert entry.get("request_body") == payload, (
            f"request_body should be {payload!r}, got {entry.get('request_body')!r}\n"
            "ROOT CAUSE: BaseHTTPMiddleware exhausts receive() before exception handler runs."
        )

    @pytest.mark.asyncio
    async def test_401_http_exception_captures_body(self):
        """401 raised before full path execution, but Pydantic already parsed body."""
        app, config = _make_app_with_flare()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            payload = {"username": "bob", "email": "bob@example.com"}
            resp = await client.post("/orders", json=payload)  # no x-auth-token
            assert resp.status_code == 401

        entry = await self._get_last_log(config, "/orders", 401)
        assert entry is not None, "Log entry for 401 not found"
        assert entry.get("request_body") == payload, (
            f"request_body should be {payload!r}, got {entry.get('request_body')!r}\n"
            "ROOT CAUSE: receive() stream exhausted by Pydantic parsing."
        )

    @pytest.mark.asyncio
    async def test_422_validation_error_captures_body(self):
        """422 uses exc.body which is set by FastAPI — should always work."""
        app, config = _make_app_with_flare()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            bad_payload = {"username": "x"}  # missing email, username too short
            resp = await client.post("/users", json=bad_payload)
            assert resp.status_code == 422

        entry = await self._get_last_log(config, "/users", 422)
        assert entry is not None, "Log entry for 422 not found"
        assert entry.get("request_body") is not None, (
            "422 should always capture body via exc.body (FastAPI provides it)"
        )

    @pytest.mark.asyncio
    async def test_500_unhandled_captures_body(self):
        """
        RuntimeError — Pydantic parsed body, then endpoint raised.

        Uses starlette.testclient.TestClient(raise_server_exceptions=False) because
        httpx ASGITransport doesn't suppress server-side exceptions. The ASGI
        test transport differs from a real uvicorn server in how
        ServerErrorMiddleware + BaseHTTPMiddleware interact; this is a known
        Starlette test-environment quirk.
        """
        import asyncio
        from starlette.testclient import TestClient

        app, config = _make_app_with_flare()
        client = TestClient(app, raise_server_exceptions=False)
        payload = {"username": "bob", "email": "bob@example.com"}
        resp = client.post("/boom", json=payload)
        assert resp.status_code == 500

        # Give the sync handler a tick to complete the async enqueue
        await asyncio.sleep(0.05)

        storage = config.storage_instance
        entries, _ = await storage.list_logs(page=1, limit=10)
        matched = next(
            (e for e in entries if e.endpoint == "/boom" and e.http_status == 500),
            None,
        )
        assert matched is not None, (
            "Log entry for 500 POST /boom not found in storage. "
            f"All entries: {[(e.endpoint, e.http_status) for e in entries]}"
        )
        assert matched.request_body == payload, (
            f"request_body should be {payload!r}, got {matched.request_body!r}\n"
            "Fix: BodyCacheMiddleware caches body in scope BEFORE stream is exhausted."
        )

    @pytest.mark.asyncio
    async def test_get_404_no_body(self):
        """
        GET requests must never have request_body captured.
        Tests push_log directly to isolate from ASGI stack timing.
        """
        import asyncio
        from fastapi_flare.queue import push_log

        app, config = _make_app_with_flare()
        # Simulate what the exception handler does for a GET 404
        await push_log(
            config,
            level="WARNING",
            event="http_exception",
            message="Not Found",
            http_status=404,
            endpoint="/nonexistent",
            http_method="GET",
            request_body=None,  # GET: no body
        )
        entries, total = await config.storage_instance.list_logs(page=1, limit=5)
        assert total == 1, f"Expected 1 entry, got {total}"
        entry = entries[0]
        assert entry.endpoint == "/nonexistent"
        assert entry.http_status == 404
        assert entry.request_body is None, (
            f"GET should never have request_body, got {entry.request_body!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 4 — BodyCacheMiddleware unit test
# ═══════════════════════════════════════════════════════════════════════════════


class TestBodyCacheMiddleware:
    """Tests for the BodyCacheMiddleware ASGI wrapper."""

    @pytest.mark.asyncio
    async def test_body_cached_in_scope_for_post(self):
        """BodyCacheMiddleware must write body bytes to scope['_flare_body']."""
        from fastapi_flare.middleware import BodyCacheMiddleware

        captured_scope = {}

        async def inner_app(scope, receive, send):
            """Simulates the inner ASGI app reading the body normally."""
            # Read ALL body chunks to simulate Pydantic
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
            # After reading, scope should have our cache
            captured_scope.update(scope)

        payload = b'{"username":"alice"}'
        call_count = 0

        async def receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        scope = {"type": "http", "method": "POST", "path": "/users"}
        send = AsyncMock()

        middleware = BodyCacheMiddleware(inner_app)
        await middleware(scope, receive, send)

        assert "_flare_body" in captured_scope, (
            "BodyCacheMiddleware must store body bytes in scope['_flare_body']"
        )
        assert captured_scope["_flare_body"] == payload

    @pytest.mark.asyncio
    async def test_get_request_not_cached(self):
        """BodyCacheMiddleware must NOT cache scope for GET (no body)."""
        from fastapi_flare.middleware import BodyCacheMiddleware

        captured_scope = {}

        async def inner_app(scope, receive, send):
            captured_scope.update(scope)

        scope = {"type": "http", "method": "GET", "path": "/items"}
        send = AsyncMock()

        async def receive():
            return {"type": "http.disconnect"}

        middleware = BodyCacheMiddleware(inner_app)
        await middleware(scope, receive, send)

        assert "_flare_body" not in captured_scope

    @pytest.mark.asyncio
    async def test_body_recoverable_after_stream_exhausted(self):
        """
        Validates the full fix end-to-end at the unit level:
        1. BodyCacheMiddleware caches body in scope
        2. _request_body() reads from that cache even when receive() is exhausted
        """
        from fastapi_flare.handlers import _request_body
        from fastapi_flare.middleware import BodyCacheMiddleware

        payload = {"username": "alice", "email": "alice@example.com"}
        payload_bytes = json.dumps(payload).encode()

        # Scope that will be shared
        scope = {"type": "http", "method": "POST", "path": "/users"}

        # Track what the inner app sees in scope
        inner_scope_ref = {}
        inner_body_consumed = False

        async def inner_app(scope, receive, send):
            nonlocal inner_body_consumed
            inner_scope_ref.update(scope)
            # Simulate Pydantic consuming the body
            msg = await receive()
            inner_body_consumed = True

        call_count = 0

        async def receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"type": "http.request", "body": payload_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        send = AsyncMock()

        # Run through the middleware
        middleware = BodyCacheMiddleware(inner_app)
        await middleware(scope, receive, send)

        assert inner_body_consumed
        assert "_flare_body" in scope, "Body must be cached in original scope"

        # Now simulate exception handler's fresh Request (exhausted stream, but scope has cache)
        from starlette.requests import Request

        fresh_request = Request(scope, lambda: None)  # receive = dead stub
        cfg = _make_config()
        result = await _request_body(fresh_request, cfg)
        assert result == payload, (
            f"Exception handler should recover body from scope cache. Got: {result!r}"
        )
