"""
In-memory request metrics aggregator for fastapi-flare.
========================================================

Updated by ``MetricsMiddleware`` on every HTTP response.
Data lives in process memory — it resets on restart.

Design goals
------------
- Zero external dependencies (only asyncio + stdlib).
- Non-blocking: the Lock is in-memory and never IO-bound.
- Simple aggregates per endpoint: count, errors, avg_latency, max_latency.
- Readable snapshot exported as a list of plain dicts (no Pydantic here,
  to keep the aggregator independent of the schema layer).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class _EndpointStats:
    count: int = 0
    errors: int = 0
    total_ms: int = 0
    max_ms: int = 0
    # Bounded ring buffer: last 1 000 samples are enough for stable P95.
    # At 500 endpoints × 1 000 samples × 4 bytes ≈ 2 MB max.
    _samples: deque = field(default_factory=lambda: deque(maxlen=1000))

    def record(self, duration_ms: int, status_code: int) -> None:
        self.count += 1
        self.total_ms += duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        if status_code >= 400:
            self.errors += 1
        self._samples.append(duration_ms)

    @property
    def avg_ms(self) -> int:
        return self.total_ms // self.count if self.count else 0

    @property
    def p95_ms(self) -> int:
        """95th-percentile latency over the last 1 000 samples."""
        if not self._samples:
            return 0
        sorted_s = sorted(self._samples)
        idx = max(0, int(len(sorted_s) * 0.95) - 1)
        return sorted_s[idx]

    @property
    def error_rate(self) -> float:
        return round(self.errors / self.count * 100, 1) if self.count else 0.0


class FlareMetrics:
    """
    Singleton-per-config in-memory metrics store.

    Holds per-endpoint aggregates (count, errors, avg latency, max latency).
    Written on every HTTP response by ``MetricsMiddleware``.
    Read at dashboard render time or by ``GET /flare/api/metrics``.

    Memory safety
    -------------
    ``max_endpoints`` caps the number of distinct keys stored (default: 500).
    Once the cap is reached, unknown new endpoints are silently dropped.
    This prevents unbounded growth from URL enumeration, scanners, or path
    parameters that bypass FastAPI's route normalisation (e.g. 404 probes).

    Usage::

        metrics = FlareMetrics()
        await metrics.record("/users", duration_ms=42, status_code=200)
        snapshot = metrics.snapshot()
        # [{"endpoint": "/users", "count": 1, "errors": 0, ...}]
    """

    def __init__(self, max_endpoints: int = 500) -> None:
        self._data: dict[str, _EndpointStats] = defaultdict(_EndpointStats)
        self._lock = asyncio.Lock()
        self._max_endpoints = max_endpoints

    async def record(self, endpoint: str, duration_ms: int, status_code: int) -> None:
        """Record one request. Called from MetricsMiddleware after every response.

        If the endpoint is already tracked, or there is room under the cap, it
        is recorded normally.  New endpoints that would exceed ``max_endpoints``
        are silently dropped to prevent memory exhaustion.
        """
        async with self._lock:
            if endpoint not in self._data and len(self._data) >= self._max_endpoints:
                return  # cap reached — drop unknown endpoint
            self._data[endpoint].record(duration_ms, status_code)

    def snapshot(self) -> list[dict]:
        """
        Return a sorted list of per-endpoint metric dicts.

        Sorted alphabetically by endpoint path.
        Safe to call without a lock — dict iteration is consistent at Python's GIL level.
        """
        return [
            {
                "endpoint": endpoint,
                "count": s.count,
                "errors": s.errors,
                "avg_latency_ms": s.avg_ms,
                "p95_latency_ms": s.p95_ms,
                "max_latency_ms": s.max_ms,
                "error_rate": s.error_rate,
            }
            for endpoint, s in sorted(self._data.items())
        ]

    @property
    def total_requests(self) -> int:
        return sum(s.count for s in self._data.values())

    @property
    def total_errors(self) -> int:
        return sum(s.errors for s in self._data.values())

    @property
    def endpoint_count(self) -> int:
        return len(self._data)

    @property
    def at_capacity(self) -> bool:
        """True when the endpoint cap has been reached."""
        return len(self._data) >= self._max_endpoints

    def reset(self) -> None:
        """Clear all accumulated metrics. Useful for tests."""
        self._data.clear()
