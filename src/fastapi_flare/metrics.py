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
- **Mergeable** latency distribution: p95/p99 is stored as a fixed-bin
  histogram so aggregates from multiple processes (uvicorn workers,
  separate pods) can be combined by summing bucket counts.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


# Upper bounds (inclusive, in milliseconds) for the latency histogram.
# Chosen to cover the full range a web request ever takes while keeping
# a constant, predictable memory footprint (~17 ints per endpoint).
# Resolution is denser where requests usually live (5ms–1s).
#
# Merging two histograms is simply ``a[i] + b[i]`` for every i, which is
# what enables cross-worker aggregation without losing percentile fidelity.
_LATENCY_BUCKETS_MS: tuple[int, ...] = (
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1000,
    2000, 5000, 10000, 30000, 60000, 300000,
)
_LATENCY_OVERFLOW_MS: int = _LATENCY_BUCKETS_MS[-1] * 2  # sentinel for >max
_BUCKET_COUNT: int = len(_LATENCY_BUCKETS_MS) + 1        # +1 for overflow


def _percentile_from_counts(counts: list[int], total: int, p: float) -> int:
    """
    Compute the *p*-th percentile (0..100) from a bucketed count list.

    Returns the upper bound of the bucket where the cumulative count first
    crosses ``total * p / 100``. Accuracy is bounded by the bucket widths
    above — for dashboard p95/p99 this is within ~2x, which is the standard
    precision for Prometheus-style histograms.
    """
    if total <= 0:
        return 0
    target = total * p / 100.0
    cumul = 0
    for i, c in enumerate(counts):
        cumul += c
        if cumul >= target:
            if i < len(_LATENCY_BUCKETS_MS):
                return _LATENCY_BUCKETS_MS[i]
            return _LATENCY_OVERFLOW_MS
    return _LATENCY_OVERFLOW_MS


@dataclass
class _EndpointStats:
    count: int = 0
    errors: int = 0
    total_ms: int = 0
    max_ms: int = 0
    # Fixed-bin latency histogram.  Each index corresponds to a bucket in
    # ``_LATENCY_BUCKETS_MS`` (last slot is the overflow bucket).
    # Size: ``_BUCKET_COUNT`` ints per endpoint — ~500 endpoints ≈ 8 KB.
    _buckets: list[int] = field(
        default_factory=lambda: [0] * _BUCKET_COUNT
    )

    def record(self, duration_ms: int, status_code: int) -> None:
        self.count += 1
        self.total_ms += duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        if status_code >= 400:
            self.errors += 1
        # Inlined bucket search — linear scan is faster than bisect for
        # 16 elements on CPython (no call overhead) and keeps the code tiny.
        for i, bound in enumerate(_LATENCY_BUCKETS_MS):
            if duration_ms <= bound:
                self._buckets[i] += 1
                return
        self._buckets[-1] += 1  # overflow

    @property
    def avg_ms(self) -> int:
        return self.total_ms // self.count if self.count else 0

    @property
    def p95_ms(self) -> int:
        """95th-percentile latency from the bucketed histogram."""
        return _percentile_from_counts(self._buckets, self.count, 95.0)

    @property
    def p99_ms(self) -> int:
        """99th-percentile latency from the bucketed histogram."""
        return _percentile_from_counts(self._buckets, self.count, 99.0)

    @property
    def error_rate(self) -> float:
        return round(self.errors / self.count * 100, 1) if self.count else 0.0

    # ── Serialisation (for cross-process persistence / merge) ─────────────

    def to_dict(self) -> dict:
        """Serialisable snapshot suitable for JSON persistence."""
        return {
            "count": self.count,
            "errors": self.errors,
            "total_ms": self.total_ms,
            "max_ms": self.max_ms,
            "buckets": list(self._buckets),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_EndpointStats":
        s = cls()
        s.count = int(data.get("count", 0))
        s.errors = int(data.get("errors", 0))
        s.total_ms = int(data.get("total_ms", 0))
        s.max_ms = int(data.get("max_ms", 0))
        buckets = data.get("buckets") or []
        if len(buckets) == _BUCKET_COUNT:
            s._buckets = [int(x) for x in buckets]
        return s

    def merge(self, other: "_EndpointStats") -> None:
        """Merge *other* into this stats object in place.

        Counts, errors, and totals add; max takes the larger; histogram
        buckets add element-wise — preserving percentile fidelity across
        workers.
        """
        self.count += other.count
        self.errors += other.errors
        self.total_ms += other.total_ms
        if other.max_ms > self.max_ms:
            self.max_ms = other.max_ms
        for i in range(_BUCKET_COUNT):
            self._buckets[i] += other._buckets[i]


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

    # ── Cross-process persistence hooks ───────────────────────────────────

    def serialize(self) -> dict:
        """Return a JSON-friendly snapshot of every endpoint's state."""
        return {
            "endpoints": {ep: s.to_dict() for ep, s in self._data.items()},
            "max_endpoints": self._max_endpoints,
        }

    def merge_serialized(self, payload: dict) -> None:
        """Merge a payload returned by :meth:`serialize` into this instance.

        New endpoints in *payload* are added up to ``max_endpoints``;
        additional ones are silently dropped (consistent with the cap
        enforced by :meth:`record`).
        """
        incoming = payload.get("endpoints") or {}
        for ep, raw in incoming.items():
            if ep not in self._data and len(self._data) >= self._max_endpoints:
                continue
            self._data[ep].merge(_EndpointStats.from_dict(raw))


async def build_merged_snapshot(config) -> tuple[list[dict], int, int, bool, int, list[str]]:
    """
    Build the aggregate view shown on the /flare/metrics dashboard.

    When ``metrics_persistence`` is disabled this simply returns the
    in-process ``FlareMetrics.snapshot()`` — same behaviour as before.

    When enabled, it:
      1. Loads every snapshot updated within ``metrics_snapshot_ttl_seconds``
         from the storage backend (one row per worker).
      2. Replaces this worker's own stored row with the live in-memory state
         (fresher than the last flush).
      3. Merges the histograms / counters across workers and returns the
         combined view — p95/p99 survive across processes and pods.

    Returns:
        ``(endpoints, total_requests, total_errors, at_capacity, worker_count, worker_ids)``
    """
    local: Optional["FlareMetrics"] = config.metrics_instance
    storage = getattr(config, "storage_instance", None)

    # No persistence, or missing deps → degrade to the local view.
    if not getattr(config, "metrics_persistence", False) or storage is None or local is None:
        if local is None:
            return ([], 0, 0, False, 0, [])
        return (
            local.snapshot(),
            local.total_requests,
            local.total_errors,
            local.at_capacity,
            1,
            [],
        )

    ttl = int(getattr(config, "metrics_snapshot_ttl_seconds", 180))
    try:
        rows = await storage.load_metrics_snapshots(since_seconds=ttl)
    except Exception:
        rows = []

    worker_instance = getattr(config, "worker_instance", None)
    local_worker_id = getattr(worker_instance, "worker_id", None)

    merged = FlareMetrics(max_endpoints=local._max_endpoints)
    seen: list[str] = []
    for worker_id, payload in rows:
        if worker_id == local_worker_id:
            continue  # prefer the live in-memory state below
        merged.merge_serialized(payload)
        seen.append(worker_id)

    merged.merge_serialized(local.serialize())
    if local_worker_id:
        seen.append(local_worker_id)

    return (
        merged.snapshot(),
        merged.total_requests,
        merged.total_errors,
        merged.at_capacity,
        len(seen),
        seen,
    )
