"""
tests/test_issues_storage.py — SQLite storage tests for issue grouping.

Exercises upsert_issue + list_issues + get_issue + list_logs_for_issue +
update_issue_status + get_issue_stats end-to-end against an in-memory SQLite.

Runs with:  poetry run pytest tests/test_issues_storage.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fastapi_flare import FlareConfig, setup
from fastapi import FastAPI


@pytest.fixture
async def storage():
    """Fresh in-memory SQLite backend per test."""
    app = FastAPI()
    cfg = FlareConfig(storage_backend="sqlite", sqlite_path=":memory:")
    setup(app, config=cfg)
    s = cfg.storage_instance
    await s._ensure_db()
    yield s
    await s.close()


def _ts(offset_s: int = 0) -> datetime:
    return datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


@pytest.mark.asyncio
async def test_first_upsert_creates_issue(storage):
    await storage.upsert_issue(
        fingerprint="abc123", exception_type="ValueError",
        endpoint="/users", sample_message="bad input",
        sample_request_id="req-1", level="ERROR", timestamp=_ts(0),
    )
    issue = await storage.get_issue("abc123")
    assert issue is not None
    assert issue.fingerprint == "abc123"
    assert issue.occurrence_count == 1
    assert issue.first_seen == _ts(0)
    assert issue.last_seen == _ts(0)
    assert issue.level == "ERROR"
    assert issue.resolved is False
    assert issue.exception_type == "ValueError"
    assert issue.endpoint == "/users"
    assert issue.sample_message == "bad input"
    assert issue.sample_request_id == "req-1"


@pytest.mark.asyncio
async def test_second_upsert_increments_count(storage):
    await storage.upsert_issue(
        fingerprint="abc", exception_type="ValueError", endpoint="/u",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(0),
    )
    await storage.upsert_issue(
        fingerprint="abc", exception_type="ValueError", endpoint="/u",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(30),
    )
    issue = await storage.get_issue("abc")
    assert issue.occurrence_count == 2
    assert issue.first_seen == _ts(0), "first_seen must be preserved"
    assert issue.last_seen == _ts(30), "last_seen must advance"


@pytest.mark.asyncio
async def test_resolved_issue_reopens_on_new_occurrence(storage):
    await storage.upsert_issue(
        fingerprint="xyz", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(0),
    )
    assert await storage.update_issue_status("xyz", resolved=True) is True
    issue = await storage.get_issue("xyz")
    assert issue.resolved is True
    assert issue.resolved_at is not None

    # new occurrence reopens
    await storage.upsert_issue(
        fingerprint="xyz", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(60),
    )
    issue = await storage.get_issue("xyz")
    assert issue.resolved is False
    assert issue.resolved_at is None
    assert issue.occurrence_count == 2


@pytest.mark.asyncio
async def test_warning_then_error_upgrades_level(storage):
    await storage.upsert_issue(
        fingerprint="fp", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="WARNING", timestamp=_ts(0),
    )
    await storage.upsert_issue(
        fingerprint="fp", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(10),
    )
    issue = await storage.get_issue("fp")
    assert issue.level == "ERROR"

    # further WARNINGs do not downgrade
    await storage.upsert_issue(
        fingerprint="fp", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="WARNING", timestamp=_ts(20),
    )
    issue = await storage.get_issue("fp")
    assert issue.level == "ERROR"


@pytest.mark.asyncio
async def test_list_issues_filters_by_resolved(storage):
    await storage.upsert_issue(
        fingerprint="a", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(0),
    )
    await storage.upsert_issue(
        fingerprint="b", exception_type="E", endpoint="/y",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(10),
    )
    await storage.update_issue_status("b", resolved=True)

    opens, opens_total = await storage.list_issues(resolved=False)
    assert opens_total == 1
    assert {i.fingerprint for i in opens} == {"a"}

    resolved, resolved_total = await storage.list_issues(resolved=True)
    assert resolved_total == 1
    assert {i.fingerprint for i in resolved} == {"b"}

    all_issues, all_total = await storage.list_issues()
    assert all_total == 2


@pytest.mark.asyncio
async def test_list_issues_order_by_last_seen_desc(storage):
    await storage.upsert_issue(
        fingerprint="old", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(0),
    )
    await storage.upsert_issue(
        fingerprint="new", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=_ts(60),
    )
    issues, _ = await storage.list_issues()
    assert issues[0].fingerprint == "new"
    assert issues[1].fingerprint == "old"


@pytest.mark.asyncio
async def test_list_issues_search_matches_exception_endpoint_message(storage):
    await storage.upsert_issue(
        fingerprint="a", exception_type="CustomException", endpoint="/x",
        sample_message="m1", sample_request_id=None, level="ERROR", timestamp=_ts(0),
    )
    await storage.upsert_issue(
        fingerprint="b", exception_type="OtherError", endpoint="/custom-path",
        sample_message="m2", sample_request_id=None, level="ERROR", timestamp=_ts(1),
    )
    await storage.upsert_issue(
        fingerprint="c", exception_type="OtherError", endpoint="/z",
        sample_message="keyword appears here", sample_request_id=None,
        level="ERROR", timestamp=_ts(2),
    )

    results_a, _ = await storage.list_issues(search="Custom")
    assert {i.fingerprint for i in results_a} == {"a", "b"}

    results_b, _ = await storage.list_issues(search="keyword")
    assert {i.fingerprint for i in results_b} == {"c"}


@pytest.mark.asyncio
async def test_list_logs_for_issue_filters_by_fingerprint(storage):
    # Enqueue two matching logs + one unrelated
    await storage.enqueue({
        "timestamp": _ts(0).isoformat(), "level": "ERROR", "event": "x",
        "message": "m", "endpoint": "/x", "issue_fingerprint": "fp1",
    })
    await storage.enqueue({
        "timestamp": _ts(1).isoformat(), "level": "ERROR", "event": "x",
        "message": "m", "endpoint": "/x", "issue_fingerprint": "fp1",
    })
    await storage.enqueue({
        "timestamp": _ts(2).isoformat(), "level": "ERROR", "event": "x",
        "message": "m", "endpoint": "/y", "issue_fingerprint": "fp2",
    })

    logs, total = await storage.list_logs_for_issue("fp1")
    assert total == 2
    assert all(l.issue_fingerprint == "fp1" for l in logs)

    logs, total = await storage.list_logs_for_issue("fp2")
    assert total == 1
    assert logs[0].issue_fingerprint == "fp2"


@pytest.mark.asyncio
async def test_update_issue_status_unknown_returns_false(storage):
    assert await storage.update_issue_status("nope", resolved=True) is False


@pytest.mark.asyncio
async def test_get_issue_stats(storage):
    now = datetime.now(tz=timezone.utc)
    await storage.upsert_issue(
        fingerprint="a", exception_type="E", endpoint="/x",
        sample_message="m", sample_request_id=None, level="ERROR", timestamp=now,
    )
    await storage.upsert_issue(
        fingerprint="b", exception_type="E", endpoint="/y",
        sample_message="m", sample_request_id=None, level="WARNING", timestamp=now,
    )
    await storage.update_issue_status("b", resolved=True)

    stats = await storage.get_issue_stats()
    assert stats.total == 2
    assert stats.open == 1
    assert stats.resolved == 1
    # both issues created moments ago — must be within 24h
    assert stats.new_last_24h == 2
    assert stats.resolved_last_7d == 1
