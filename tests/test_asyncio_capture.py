"""
tests/test_asyncio_capture.py — Benign event-loop noise filtering.

Covers the fix for the production "Future exception was never retrieved" spam
reported against the PostgreSQL backend (ConnectionDoesNotExistError +
CancelledError):

  - _is_benign_loop_noise() classification (CancelledError, asyncpg
    connection-drop errors by name/message, real errors pass through)
  - install_asyncio_capture(): benign noise is NOT forwarded to storage and
    the previous/default handler is NOT invoked (breaking the feedback loop),
    while genuine errors still flow through both paths.

Runs with:  poetry run pytest tests/test_asyncio_capture.py -v
"""
from __future__ import annotations

import asyncio

import pytest

from fastapi_flare.integrations.logging import (
    _is_benign_loop_noise,
    install_asyncio_capture,
)


# ── _is_benign_loop_noise ──────────────────────────────────────────────────────

class ConnectionDoesNotExistError(Exception):
    """Stand-in matching asyncpg's class name without importing asyncpg."""


def test_cancelled_error_is_benign():
    assert _is_benign_loop_noise(asyncio.CancelledError(), "") is True


def test_connection_dropped_by_class_name_is_benign():
    exc = ConnectionDoesNotExistError("boom")
    assert _is_benign_loop_noise(exc, "") is True


def test_connection_dropped_by_message_is_benign():
    exc = RuntimeError("connection was closed in the middle of operation")
    assert _is_benign_loop_noise(exc, "") is True
    # also when the phrase is only in the context message, not the exception
    assert _is_benign_loop_noise(
        None, "connection was closed in the middle of operation"
    ) is True


def test_real_error_is_not_benign():
    assert _is_benign_loop_noise(ValueError("bad input"), "task failed") is False
    assert _is_benign_loop_noise(None, "Task was destroyed but it is pending") is False


# ── install_asyncio_capture handler behaviour ──────────────────────────────────

class _RecordingStorage:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def enqueue(self, entry: dict) -> None:
        self.entries.append(entry)

    async def upsert_issue(self, **kwargs) -> None:  # noqa: D401
        pass


def _make_config(storage):
    from fastapi_flare import FlareConfig

    class _Cfg(FlareConfig):
        model_config = {**FlareConfig.model_config, "env_file": None}

    cfg = _Cfg(alert_notifiers=[])
    cfg.storage_instance = storage
    return cfg


@pytest.mark.asyncio
async def test_benign_noise_is_dropped_and_does_not_reach_storage():
    storage = _RecordingStorage()
    config = _make_config(storage)

    loop = asyncio.get_running_loop()
    prior_calls: list[dict] = []
    loop.set_exception_handler(lambda _l, ctx: prior_calls.append(ctx))

    install_asyncio_capture(config)

    # Simulate the loop reporting a dropped pooled connection.
    loop.call_exception_handler(
        {
            "message": "Future exception was never retrieved",
            "exception": ConnectionDoesNotExistError(
                "connection was closed in the middle of operation"
            ),
        }
    )
    # Let any (incorrectly) scheduled forwarding task run.
    await asyncio.sleep(0)

    assert storage.entries == []          # not forwarded to storage
    assert prior_calls == []              # previous handler not invoked → no re-log


@pytest.mark.asyncio
async def test_real_error_is_forwarded_and_chains_to_previous_handler():
    storage = _RecordingStorage()
    config = _make_config(storage)

    loop = asyncio.get_running_loop()
    prior_calls: list[dict] = []
    loop.set_exception_handler(lambda _l, ctx: prior_calls.append(ctx))

    install_asyncio_capture(config)

    loop.call_exception_handler(
        {"message": "task failed", "exception": ValueError("genuine bug")}
    )
    await asyncio.sleep(0)  # allow the fire-and-forget push_log task to run

    assert len(storage.entries) == 1
    assert storage.entries[0]["error"] == "ValueError: genuine bug"
    assert len(prior_calls) == 1          # previous handler still chained
