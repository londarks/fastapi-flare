"""
tests/test_fingerprint.py — Unit tests for the issue-fingerprint algorithm.

Runs with:  poetry run pytest tests/test_fingerprint.py -v
"""
from __future__ import annotations

from fastapi_flare.fingerprint import (
    _extract_exception_type,
    _parse_stack_frames,
    compute_fingerprint,
)


_TRACE_A = """Traceback (most recent call last):
  File "/srv/app/api/users.py", line 42, in create_user
    result = run()
  File "/srv/app/services/user.py", line 100, in run
    return do_work()
  File "/srv/app/services/user.py", line 95, in do_work
    raise ValueError("invalid input")
ValueError: invalid input
"""

_TRACE_A_LINES_SHIFTED = """Traceback (most recent call last):
  File "/srv/app/api/users.py", line 58, in create_user
    result = run()
  File "/srv/app/services/user.py", line 120, in run
    return do_work()
  File "/srv/app/services/user.py", line 115, in do_work
    raise ValueError("invalid input")
ValueError: invalid input
"""

_TRACE_A_DIFFERENT_FILE = """Traceback (most recent call last):
  File "/srv/app/api/orders.py", line 42, in create_order
    result = run()
  File "/srv/app/services/order.py", line 100, in run
    return do_work()
  File "/srv/app/services/order.py", line 95, in do_work
    raise ValueError("invalid input")
ValueError: invalid input
"""


# ── _extract_exception_type ─────────────────────────────────────────────────

class TestExtractExceptionType:
    def test_standard_format(self):
        assert _extract_exception_type("ValueError: bad") == "ValueError"

    def test_no_message(self):
        assert _extract_exception_type("RuntimeError") == "RuntimeError"

    def test_nested_colons(self):
        assert _extract_exception_type("KeyError: 'foo: bar'") == "KeyError"

    def test_none(self):
        assert _extract_exception_type(None) is None

    def test_empty(self):
        assert _extract_exception_type("") is None


# ── _parse_stack_frames ─────────────────────────────────────────────────────

class TestParseStackFrames:
    def test_parses_all_frames(self):
        frames = _parse_stack_frames(_TRACE_A, limit=10)
        assert frames == [
            ("users.py", "create_user"),
            ("user.py", "run"),
            ("user.py", "do_work"),
        ]

    def test_limit_takes_last_n(self):
        frames = _parse_stack_frames(_TRACE_A, limit=2)
        assert frames == [("user.py", "run"), ("user.py", "do_work")]

    def test_no_frames_returns_empty(self):
        frames = _parse_stack_frames("something random", limit=5)
        assert frames == []


# ── compute_fingerprint — stacktrace path ───────────────────────────────────

class TestFingerprintStacktrace:
    def test_same_trace_same_fp(self):
        a = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A, endpoint="/users", http_status=500,
        )
        b = compute_fingerprint(
            event="unhandled_exception", error="ValueError: y",
            stack_trace=_TRACE_A, endpoint="/users", http_status=500,
        )
        assert a == b, "Different error messages must still group together"

    def test_line_number_shift_keeps_fp(self):
        """Trivial refactors that move code down should not re-fingerprint."""
        a = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A, endpoint="/users", http_status=500,
        )
        b = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A_LINES_SHIFTED, endpoint="/users", http_status=500,
        )
        assert a == b, "Line numbers must not be part of the fingerprint"

    def test_different_file_different_fp(self):
        a = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A, endpoint="/users", http_status=500,
        )
        b = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A_DIFFERENT_FILE, endpoint="/orders", http_status=500,
        )
        assert a != b, "Different files/endpoints must produce different issues"

    def test_different_exception_type_different_fp(self):
        a = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A, endpoint="/users", http_status=500,
        )
        b = compute_fingerprint(
            event="unhandled_exception", error="KeyError: x",
            stack_trace=_TRACE_A, endpoint="/users", http_status=500,
        )
        assert a != b

    def test_different_endpoint_different_fp(self):
        a = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A, endpoint="/users", http_status=500,
        )
        b = compute_fingerprint(
            event="unhandled_exception", error="ValueError: x",
            stack_trace=_TRACE_A, endpoint="/orders", http_status=500,
        )
        assert a != b, "Same bug on different endpoints should be different issues"


# ── compute_fingerprint — HTTP path (no stacktrace) ─────────────────────────

class TestFingerprintHttp:
    def test_same_404_same_endpoint(self):
        a = compute_fingerprint(
            event="http_exception", error="HTTPException 404: Not Found",
            stack_trace=None, endpoint="/items/{id}", http_status=404,
        )
        b = compute_fingerprint(
            event="http_exception", error="HTTPException 404: Missing",
            stack_trace=None, endpoint="/items/{id}", http_status=404,
        )
        assert a == b

    def test_404_vs_500_different(self):
        a = compute_fingerprint(
            event="http_exception", error="",
            stack_trace=None, endpoint="/x", http_status=404,
        )
        b = compute_fingerprint(
            event="http_exception", error="",
            stack_trace=None, endpoint="/x", http_status=500,
        )
        assert a != b

    def test_different_endpoints_different(self):
        a = compute_fingerprint(
            event="http_exception", error="",
            stack_trace=None, endpoint="/a", http_status=404,
        )
        b = compute_fingerprint(
            event="http_exception", error="",
            stack_trace=None, endpoint="/b", http_status=404,
        )
        assert a != b


# ── compute_fingerprint — validation path ───────────────────────────────────

class TestFingerprintValidation:
    def test_same_endpoint_same(self):
        a = compute_fingerprint(
            event="validation_error", error="username: Field required",
            stack_trace=None, endpoint="/signup", http_status=422,
        )
        b = compute_fingerprint(
            event="validation_error", error="email: Invalid",
            stack_trace=None, endpoint="/signup", http_status=422,
        )
        assert a == b, "All validation errors on one endpoint collapse to one issue"

    def test_different_endpoints_different(self):
        a = compute_fingerprint(
            event="validation_error", error="x",
            stack_trace=None, endpoint="/a", http_status=422,
        )
        b = compute_fingerprint(
            event="validation_error", error="x",
            stack_trace=None, endpoint="/b", http_status=422,
        )
        assert a != b


# ── Output format ────────────────────────────────────────────────────────────

class TestFingerprintFormat:
    def test_hex_length_16(self):
        fp = compute_fingerprint(
            event="http_exception", error="",
            stack_trace=None, endpoint="/x", http_status=500,
        )
        assert len(fp) == 16
        int(fp, 16)  # raises if not valid hex

    def test_deterministic_across_calls(self):
        kwargs = dict(
            event="http_exception", error="", stack_trace=None,
            endpoint="/x", http_status=500,
        )
        assert compute_fingerprint(**kwargs) == compute_fingerprint(**kwargs)
