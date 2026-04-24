"""
tests/test_response_body.py — Response body capture (opt-in).

Covers:
  - off by default
  - on: captures when status >= threshold
  - off below threshold
  - respects max_response_body_bytes cap
  - sensitive fields masked in error responses
  - binary / streaming responses skipped (don't break streaming)
  - retention null-out clears old response_body but keeps row

Note: we avoid ``with TestClient(...)`` because the context manager triggers the
app lifespan, which stops the worker and closes the storage — we'd then try to
query a closed DB.  Using ``TestClient`` without ``with`` keeps the storage alive.

Runs with:  poetry run pytest tests/test_response_body.py -v
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.testclient import TestClient


def _make_app(**cfg_overrides):
    from fastapi_flare import FlareConfig, setup

    class _Cfg(FlareConfig):
        model_config = {**FlareConfig.model_config, "env_file": None}

    app = FastAPI()
    config = _Cfg(
        storage_backend="sqlite",
        sqlite_path=":memory:",
        **cfg_overrides,
    )
    setup(app, config=config)
    return app, config


async def _settle():
    # Give the asyncio.create_task inside RequestTrackingMiddleware time to run.
    # TestClient runs the app in its own loop; the fire-and-forget enqueue may
    # not complete before control returns to the test — yield repeatedly.
    for _ in range(20):
        await asyncio.sleep(0.02)


async def _last_request(config, path: str):
    await _settle()
    entries, _ = await config.storage_instance.list_requests(page=1, limit=10)
    return next((e for e in entries if e.path == path), None)


async def _last_log(config, endpoint: str):
    await _settle()
    entries, _ = await config.storage_instance.list_logs(page=1, limit=10)
    return next((e for e in entries if e.endpoint == endpoint), None)


# ── basics ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_off_by_default():
    app, config = _make_app()

    @app.get("/err")
    async def err():
        raise HTTPException(status_code=500, detail="bad")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/err")

    req = await _last_request(config, "/err")
    log = await _last_log(config, "/err")
    assert req is not None and req.response_body is None
    assert log is not None and log.response_body is None


@pytest.mark.asyncio
async def test_captures_error_response_when_enabled():
    app, config = _make_app(capture_response_body=True)

    @app.get("/err")
    async def err():
        raise HTTPException(status_code=500, detail="explosive")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/err")

    req = await _last_request(config, "/err")
    log = await _last_log(config, "/err")
    assert req.response_body == {"detail": "explosive"}
    assert log.response_body == {"detail": "explosive"}


@pytest.mark.asyncio
async def test_skips_below_threshold():
    # Default threshold = 400 — a 200 OK is excluded.
    app, config = _make_app(capture_response_body=True, track_2xx_requests=True)

    @app.get("/ok")
    async def ok():
        return {"hello": "world"}

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/ok")

    req = await _last_request(config, "/ok")
    assert req is not None
    assert req.response_body is None, "200 is below default 400 threshold"


@pytest.mark.asyncio
async def test_captures_everything_when_threshold_lowered():
    app, config = _make_app(
        capture_response_body=True,
        capture_response_body_min_status=200,
        track_2xx_requests=True,
    )

    @app.get("/ok")
    async def ok():
        return {"hello": "world"}

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/ok")

    req = await _last_request(config, "/ok")
    assert req is not None
    assert req.response_body == {"hello": "world"}


@pytest.mark.asyncio
async def test_truncated_at_max_bytes():
    app, config = _make_app(
        capture_response_body=True,
        capture_response_body_min_status=200,
        max_response_body_bytes=20,
        track_2xx_requests=True,
    )

    @app.get("/big")
    async def big():
        return {"payload": "x" * 500}

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/big")

    req = await _last_request(config, "/big")
    assert req is not None
    # Truncated JSON is unlikely to parse — stored as decoded text.
    assert isinstance(req.response_body, str)
    assert len(req.response_body) == 20


@pytest.mark.asyncio
async def test_sensitive_fields_masked_in_error_response():
    app, config = _make_app(capture_response_body=True)

    @app.get("/secret-err")
    async def secret():
        raise HTTPException(status_code=400, detail={"token": "abc123", "info": "nope"})

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/secret-err")

    log = await _last_log(config, "/secret-err")
    assert log is not None
    assert log.response_body["detail"]["token"] == "***REDACTED***"
    assert log.response_body["detail"]["info"] == "nope"


# ── streaming / binary safety ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_streaming_response_bytes_intact():
    """A StreamingResponse's bytes must reach the client unchanged.

    Note: BaseHTTPMiddleware in Starlette already buffers all responses
    internally, so "don't break streaming" means preserving the payload, not
    avoiding the buffer.  Response body capture is opt-in, so buffering text
    for storage is acceptable — the client still receives every byte.
    """
    app, config = _make_app(
        capture_response_body=True,
        capture_response_body_min_status=200,
        track_2xx_requests=True,
    )

    async def generate():
        for i in range(5):
            yield f"chunk-{i}\n".encode()

    @app.get("/stream")
    async def stream():
        return StreamingResponse(generate(), media_type="text/plain")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/stream")
    assert resp.text == "chunk-0\nchunk-1\nchunk-2\nchunk-3\nchunk-4\n"


@pytest.mark.asyncio
async def test_sse_content_type_skipped():
    """text/event-stream responses must be skipped from capture."""
    app, config = _make_app(
        capture_response_body=True,
        capture_response_body_min_status=200,
        track_2xx_requests=True,
    )

    async def generate():
        yield b"data: one\n\n"
        yield b"data: two\n\n"

    @app.get("/sse")
    async def sse():
        return StreamingResponse(generate(), media_type="text/event-stream")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/sse")

    req = await _last_request(config, "/sse")
    assert req is not None
    assert req.response_body is None, "text/event-stream must be skipped"


@pytest.mark.asyncio
async def test_binary_content_type_skipped():
    app, config = _make_app(
        capture_response_body=True,
        capture_response_body_min_status=200,
        track_2xx_requests=True,
    )

    @app.get("/img")
    async def img():
        return Response(content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
                        media_type="image/png")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/img")

    req = await _last_request(config, "/img")
    assert req is not None
    assert req.response_body is None, "image/* content-type must be skipped"


# ── retention null-out ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retention_nulls_out_old_response_bodies():
    app, config = _make_app(
        capture_response_body=True,
        response_body_retention_hours=1,
        retention_check_interval_minutes=0,
    )

    @app.get("/err")
    async def err():
        raise HTTPException(status_code=500, detail="boom")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/err")

    log = await _last_log(config, "/err")
    assert log.response_body == {"detail": "boom"}

    # Back-date the row so flush() will null out response_body
    storage = config.storage_instance
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()
    db = await storage._ensure_db()
    await db.execute("UPDATE logs SET timestamp = ? WHERE endpoint = ?", (past, "/err"))
    await db.execute("UPDATE requests SET timestamp = ? WHERE path = ?", (past, "/err"))
    await db.commit()

    await storage.flush()

    entries, _ = await storage.list_logs(page=1, limit=10)
    row = next(e for e in entries if e.endpoint == "/err")
    assert row.response_body is None, "response_body must be nulled after TTL"
    assert row.error is not None, "row itself must survive"
