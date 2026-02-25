"""
test_error_capture.py — Functional correctness test for fastapi-flare.

Verifica se TODOS os erros >= 400 são capturados corretamente, incluindo:
  - http_status correto
  - event field   (http_exception | validation_error | unhandled_exception)
  - level field   (WARNING para 4xx, ERROR para 5xx)
  - request_body  presente nos métodos POST/DELETE com body

Pré-requisitos:
    poetry run uvicorn examples.example:app --reload --port 8001

Execução:
    poetry run python examples/test_error_capture.py
    poetry run python examples/test_error_capture.py --base-url http://localhost:8001
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = "http://localhost:8001"
FLARE_LOGS_URL = f"{BASE_URL}/flare/api/logs"
WORKER_FLUSH_WAIT = 1.5   # seconds — worker flushes every ~1 s
SEARCH_WINDOW = 30        # look at last N log entries when searching
TIMEOUT = httpx.Timeout(10.0)

# ANSI colours
_G = "\033[92m"  # green
_R = "\033[91m"  # red
_Y = "\033[93m"  # yellow
_B = "\033[96m"  # cyan
_W = "\033[97m"  # white bold
_X = "\033[0m"   # reset


# ─── Test case definition ────────────────────────────────────────────────────

@dataclass
class Case:
    name: str
    method: str
    path: str
    expected_status: int
    expected_event: str
    expected_level: str
    expect_body: bool = False        # should request_body be captured?
    json_body: dict | None = None
    headers: dict | None = None
    skip_log_check: bool = False     # some 405s may not be routed through handlers


CASES: list[Case] = [
    # ── GET errors ─────────────────────────────────────────────────────────
    Case(
        name="404 item not found",
        method="GET",
        path="/items/999",
        expected_status=404,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=False,
    ),
    Case(
        name="403 forbidden (no token)",
        method="GET",
        path="/admin",
        expected_status=403,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=False,
    ),
    Case(
        name="500 unhandled GET /boom",
        method="GET",
        path="/boom",
        expected_status=500,
        expected_event="unhandled_exception",
        expected_level="ERROR",
        expect_body=False,
    ),
    # ── POST /users ─────────────────────────────────────────────────────────
    Case(
        name="422 validation — POST /users bad body",
        method="POST",
        path="/users",
        expected_status=422,
        expected_event="validation_error",
        expected_level="WARNING",
        expect_body=True,
        json_body={"username": "x", "email": "not-an-email"},   # username too short
    ),
    Case(
        name="409 conflict — POST /users duplicate username",
        method="POST",
        path="/users",
        expected_status=409,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=True,
        json_body={"username": "alice", "email": "alice2@example.com"},
    ),
    # ── POST /orders ─────────────────────────────────────────────────────────
    Case(
        name="401 unauthorized — POST /orders no token",
        method="POST",
        path="/orders",
        expected_status=401,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=True,
        json_body={"user_id": 1, "product": "Laptop", "quantity": 1},
    ),
    Case(
        name="404 user not found — POST /orders bad user",
        method="POST",
        path="/orders",
        expected_status=404,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=True,
        json_body={"user_id": 9999, "product": "Phone", "quantity": 1},
        headers={"x-auth-token": "anything"},
    ),
    Case(
        name="400 invalid coupon — POST /orders",
        method="POST",
        path="/orders",
        expected_status=400,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=True,
        json_body={"user_id": 1, "product": "Tablet", "quantity": 1, "coupon": "FAKE50"},
        headers={"x-auth-token": "anything"},
    ),
    Case(
        name="422 validation — POST /orders bad quantity",
        method="POST",
        path="/orders",
        expected_status=422,
        expected_event="validation_error",
        expected_level="WARNING",
        expect_body=True,
        json_body={"user_id": 1, "product": "Watch", "quantity": 0, "coupon": None},  # quantity < 1
        headers={"x-auth-token": "anything"},
    ),
    # ── POST /payments ────────────────────────────────────────────────────────
    Case(
        name="404 order not found — POST /payments",
        method="POST",
        path="/payments",
        expected_status=404,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=True,
        json_body={"order_id": 8888, "amount": 100.0, "method": "pix"},
    ),
    Case(
        name="402 insufficient amount — POST /payments",
        method="POST",
        path="/payments",
        expected_status=402,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=True,
        json_body={"order_id": 100, "amount": 1.0, "method": "credit"},
    ),
    Case(
        name="500 billing engine fault — POST /payments amount=13.37",
        method="POST",
        path="/payments",
        expected_status=500,
        expected_event="unhandled_exception",
        expected_level="ERROR",
        expect_body=True,
        json_body={"order_id": 100, "amount": 13.37, "method": "debit"},
    ),
    Case(
        name="422 validation — POST /payments bad method",
        method="POST",
        path="/payments",
        expected_status=422,
        expected_event="validation_error",
        expected_level="WARNING",
        expect_body=True,
        json_body={"order_id": 100, "amount": 50.0, "method": "bitcoin"},  # not in pattern
    ),
    # ── DELETE ────────────────────────────────────────────────────────────────
    Case(
        name="404 item not found — DELETE /items/999",
        method="DELETE",
        path="/items/999",
        expected_status=404,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=False,
    ),
    # ── Unknown route ─────────────────────────────────────────────────────────
    Case(
        name="404 unknown route — GET /nonexistent",
        method="GET",
        path="/this-route-does-not-exist-at-all",
        expected_status=404,
        expected_event="http_exception",
        expected_level="WARNING",
        expect_body=False,
    ),
]


# ─── Helpers ─────────────────────────────────────────────────────────────────

@dataclass
class Result:
    case: Case
    trigger_status: int
    passed: bool
    failures: list[str] = field(default_factory=list)
    log_entry: dict | None = None


async def trigger_error(client: httpx.AsyncClient, case: Case) -> int:
    """Fire the request and return the actual HTTP status code."""
    kwargs: dict[str, Any] = {"headers": case.headers or {}}
    if case.json_body is not None:
        kwargs["json"] = case.json_body
    resp = await client.request(case.method, f"{BASE_URL}{case.path}", **kwargs)
    return resp.status_code


async def fetch_logs(client: httpx.AsyncClient, limit: int = SEARCH_WINDOW) -> list[dict]:
    resp = await client.get(FLARE_LOGS_URL, params={"limit": limit})
    resp.raise_for_status()
    data = resp.json()
    return data.get("logs", [])


def find_matching_log(logs: list[dict], case: Case, after_ts: float) -> dict | None:
    """
    Find the most recent log entry that matches this case.
    Matches on: endpoint == case.path AND http_status == expected_status.
    FlareLogEntry serialises the path as 'endpoint' (not 'path').
    """
    for entry in logs:  # newest first (XREVRANGE order)
        # FlareLogEntry field is 'endpoint'; accept 'path' as legacy fallback
        ep = entry.get("endpoint") or entry.get("path", "")
        status = entry.get("http_status")
        if ep == case.path and status == case.expected_status:
            return entry
    return None


def check_entry(case: Case, entry: dict) -> list[str]:
    """Return a list of failure strings (empty = all good)."""
    failures: list[str] = []

    # event
    got_event = entry.get("event")
    if got_event != case.expected_event:
        failures.append(f"event: expected={case.expected_event!r} got={got_event!r}")

    # level
    got_level = entry.get("level")
    if got_level != case.expected_level:
        failures.append(f"level: expected={case.expected_level!r} got={got_level!r}")

    # http_status
    got_status = entry.get("http_status")
    if got_status != case.expected_status:
        failures.append(f"http_status: expected={case.expected_status} got={got_status}")

    # request_body presence
    has_body = entry.get("request_body") is not None
    if case.expect_body and not has_body:
        failures.append("request_body: expected to be captured but was None/missing")
    if not case.expect_body and has_body:
        failures.append(f"request_body: expected None but got {entry['request_body']!r}")

    return failures


# ─── Main test runner ─────────────────────────────────────────────────────────

async def run_tests(base_url: str) -> bool:
    global BASE_URL, FLARE_LOGS_URL
    BASE_URL = base_url
    FLARE_LOGS_URL = f"{base_url}/flare/api/logs"

    print(f"\n{_W}fastapi-flare — Functional Error Capture Test{_X}")
    print(f"{_B}Target: {BASE_URL}{_X}\n")

    # Verify the app is up
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.get(f"{BASE_URL}/")
            assert r.status_code == 200, f"Root returned {r.status_code}"
        except Exception as exc:
            print(f"{_R}✗ Cannot reach {BASE_URL}/ — {exc}{_X}")
            print(f"  Start the server first:  poetry run uvicorn examples.example:app --reload --port 8001")
            return False

        print(f"{_G}✓ App is reachable{_X}\n")
        print(f"{'Case':<58} {'HTTP':>4}  {'Log':>6}  {'Result':<8}")
        print("─" * 90)

        results: list[Result] = []

        for case in CASES:
            # Fire the request
            try:
                actual_status = await trigger_error(client, case)
            except Exception as exc:
                r = Result(case=case, trigger_status=0, passed=False,
                           failures=[f"Request failed: {exc}"])
                results.append(r)
                _print_row(case, 0, None, r.failures)
                continue

            if actual_status != case.expected_status:
                r = Result(case=case, trigger_status=actual_status, passed=False,
                           failures=[f"Expected HTTP {case.expected_status}, got {actual_status}"])
                results.append(r)
                _print_row(case, actual_status, None, r.failures)
                continue

            # Allow worker to flush
            await asyncio.sleep(WORKER_FLUSH_WAIT)

            if case.skip_log_check:
                r = Result(case=case, trigger_status=actual_status, passed=True)
                results.append(r)
                _print_row(case, actual_status, None, [])
                continue

            # Fetch logs and find match
            logs = await fetch_logs(client)
            entry = find_matching_log(logs, case, after_ts=time.time() - 10)

            if entry is None:
                r = Result(case=case, trigger_status=actual_status, passed=False,
                           failures=["Log entry NOT FOUND in /flare/api/logs"])
                results.append(r)
                _print_row(case, actual_status, None, r.failures)
                continue

            failures = check_entry(case, entry)
            r = Result(case=case, trigger_status=actual_status, passed=len(failures) == 0,
                       failures=failures, log_entry=entry)
            results.append(r)
            _print_row(case, actual_status, entry, failures)

        # ── Summary ────────────────────────────────────────────────────────
        print("\n" + "─" * 90)
        passed = sum(1 for r in results if r.passed)
        failed = len(results) - passed

        if failed == 0:
            print(f"\n{_G}✓ ALL {passed} TESTS PASSED{_X}\n")
        else:
            print(f"\n{_R}✗ {failed} FAILED  /  {passed} PASSED  (total {len(results)}){_X}\n")
            for r in results:
                if not r.passed:
                    print(f"  {_R}FAIL{_X}  {r.case.name}")
                    for f in r.failures:
                        print(f"        • {f}")
            print()

        return failed == 0


def _print_row(case: Case, status: int, entry: dict | None, failures: list[str]) -> None:
    found = "found" if entry is not None else (f"{_Y}missing{_X}" if not case.skip_log_check else "skip")
    ok = f"{_G}PASS{_X}" if not failures else f"{_R}FAIL{_X}"
    status_color = _G if status == case.expected_status else _R
    print(
        f"  {case.name:<56} "
        f"{status_color}{status:>4}{_X}  "
        f"{found:>10}  "
        f"{ok}"
    )
    for f in failures:
        print(f"    {_Y}↳ {f}{_X}")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="fastapi-flare functional test")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8001",
        help="Base URL of the running example app (default: http://localhost:8001)",
    )
    args = parser.parse_args()

    ok = asyncio.run(run_tests(args.base_url))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
