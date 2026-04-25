"""
tests/test_request_buffer.py — Batched request inserts (request_buffer_size > 0).

Covers:
  - default behaviour (buffer_size=0): immediate INSERT, list_requests sees row
  - buffered: append to memory, list_requests stays empty until flush
  - flush_request_buffer drains everything in one shot
  - reaching buffer_size triggers an immediate flush
  - close() drains pending entries

Runs with:  poetry run pytest tests/test_request_buffer.py -v
"""
from __future__ import annotations

import asyncio

import pytest


def _make_storage(**cfg):
    from fastapi_flare import FlareConfig
    from fastapi_flare.storage import make_storage

    class _Cfg(FlareConfig):
        model_config = {**FlareConfig.model_config, "env_file": None}

    defaults = {
        "storage_backend": "sqlite",
        "sqlite_path": ":memory:",
        "track_requests": True,
        "track_2xx_requests": True,  # so 200 entries land in the table
    }
    defaults.update(cfg)
    config = _Cfg(**defaults)
    return make_storage(config), config


def _entry(path: str, status: int = 200, **extra) -> dict:
    return {
        "method": "GET",
        "path": path,
        "status_code": status,
        "duration_ms": 5,
        "request_id": f"req-{path}",
        "ip_address": "127.0.0.1",
        "user_agent": "test",
        **extra,
    }


@pytest.mark.asyncio
async def test_immediate_when_buffer_size_zero():
    storage, _ = _make_storage(request_buffer_size=0)
    await storage.enqueue_request(_entry("/a"))
    rows, total = await storage.list_requests(page=1, limit=10)
    assert total == 1
    assert rows[0].path == "/a"
    await storage.close()


@pytest.mark.asyncio
async def test_buffered_writes_not_visible_until_flush():
    storage, _ = _make_storage(request_buffer_size=10)
    await storage.enqueue_request(_entry("/a"))
    await storage.enqueue_request(_entry("/b"))

    # Buffer holds 2 entries; nothing flushed yet
    assert len(storage._req_buffer) == 2
    rows, total = await storage.list_requests(page=1, limit=10)
    assert total == 0, "buffered rows must not appear in list_requests until flush"

    # Manual flush
    flushed = await storage.flush_request_buffer()
    assert flushed == 2
    assert len(storage._req_buffer) == 0

    rows, total = await storage.list_requests(page=1, limit=10)
    assert total == 2
    assert {r.path for r in rows} == {"/a", "/b"}
    await storage.close()


@pytest.mark.asyncio
async def test_buffer_size_triggers_auto_flush():
    storage, _ = _make_storage(request_buffer_size=3)
    await storage.enqueue_request(_entry("/a"))
    await storage.enqueue_request(_entry("/b"))
    # Still under the threshold
    assert len(storage._req_buffer) == 2
    rows, total = await storage.list_requests(page=1, limit=10)
    assert total == 0

    # Third entry hits the threshold and auto-flushes
    await storage.enqueue_request(_entry("/c"))
    assert len(storage._req_buffer) == 0, "buffer must drain when size hit"
    rows, total = await storage.list_requests(page=1, limit=10)
    assert total == 3
    await storage.close()


@pytest.mark.asyncio
async def test_close_drains_buffer():
    storage, _ = _make_storage(request_buffer_size=100)
    await storage.enqueue_request(_entry("/a"))
    await storage.enqueue_request(_entry("/b"))
    assert len(storage._req_buffer) == 2

    # close() should flush before tearing down the connection
    await storage.close()

    # Re-open the same memory DB? :memory: is per-connection — we can't query
    # after close to verify rows landed.  Instead we assert the buffer is empty
    # (close() has run flush_request_buffer at least once).
    assert len(storage._req_buffer) == 0


@pytest.mark.asyncio
async def test_flush_empty_buffer_returns_zero():
    storage, _ = _make_storage(request_buffer_size=10)
    flushed = await storage.flush_request_buffer()
    assert flushed == 0
    await storage.close()


@pytest.mark.asyncio
async def test_buffered_path_skipped_when_track_requests_off():
    storage, _ = _make_storage(request_buffer_size=10, track_requests=False)
    await storage.enqueue_request(_entry("/a"))
    # Disabled track_requests bypasses the buffer entirely
    assert storage._req_buffer == []
    rows, total = await storage.list_requests(page=1, limit=10)
    assert total == 0
    await storage.close()
