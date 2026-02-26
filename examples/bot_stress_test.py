"""
fastapi-flare — Bot / stress test simulator
============================================

Simulates multiple attack and traffic patterns against the running app.
Run the app first:

    poetry run uvicorn examples.example:app --reload --port 8001

Then in another terminal:

    poetry run python examples/bot_stress_test.py

Phases
------
1. Normal traffic       — legit requests to valid endpoints
2. URL enumeration      — /items/1 … /items/5000  (tests cap + normalization)
3. 404 probe burst      — random non-existent paths  (tests <unmatched> key)
4. Error flood          — repeated /boom  (fills error log)
5. Concurrent burst     — 200 simultaneous requests  (tests concurrency safety)
6. Stats poll           — reads /flare/api/metrics after each phase
"""

from __future__ import annotations

import asyncio
import random
import string
import time
from typing import Callable

import httpx

BASE = "http://localhost:8002"
METRICS_URL = f"{BASE}/flare/api/metrics"

# ── ANSI colors ──────────────────────────────────────────────────────────────
R  = "\033[91m"
G  = "\033[92m"
Y  = "\033[93m"
B  = "\033[94m"
M  = "\033[95m"
C  = "\033[96m"
W  = "\033[97m"
DIM = "\033[2m"
RST = "\033[0m"
BOLD = "\033[1m"

def _fmt_status(status: int) -> str:
    if status < 300:
        return f"{G}{status}{RST}"
    if status < 400:
        return f"{Y}{status}{RST}"
    if status < 500:
        return f"{Y}{status}{RST}"
    return f"{R}{status}{RST}"

def _bar(value: int, max_value: int, width: int = 30, color: str = G) -> str:
    filled = int(width * value / max(max_value, 1))
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RST}"


# ── Metrics snapshot printer ─────────────────────────────────────────────────

async def print_metrics(client: httpx.AsyncClient, label: str) -> None:
    try:
        r = await client.get(METRICS_URL, timeout=5)
        data = r.json()
    except Exception as e:
        print(f"  {R}[metrics error] {e}{RST}")
        return

    endpoints  = data.get("endpoints", [])
    total_req  = data.get("total_requests", 0)
    total_err  = data.get("total_errors", 0)
    at_cap     = data.get("at_capacity", False)
    max_ep     = data.get("max_endpoints", 500)
    rate       = round(total_err / total_req * 100, 1) if total_req else 0

    cap_warn = f"  {Y}⚠ CAP REACHED ({len(endpoints)}/{max_ep}){RST}" if at_cap else f"  {G}cap ok ({len(endpoints)}/{max_ep}) {RST}"

    print(f"\n  {BOLD}{C}── Metrics after: {label} ─────────────────────────────{RST}")
    print(f"  Total requests : {BOLD}{W}{total_req:>6}{RST}  |  "
          f"Errors: {R}{total_err:>5}{RST}  |  "
          f"Error rate: {'%s%.1f%%%s' % (R if rate > 20 else Y if rate > 5 else G, rate, RST)}")
    print(f"  Tracked routes : {len(endpoints)} {cap_warn}")

    if endpoints:
        print(f"  {DIM}{'Endpoint':<35} {'Reqs':>6}  {'Err':>5}  {'Rate':>6}  {'Avg':>7}  {'Max':>7}{RST}")
        for ep in sorted(endpoints, key=lambda e: e["count"], reverse=True)[:12]:
            rate_ep = ep["error_rate"]
            rc = R if rate_ep > 20 else Y if rate_ep > 5 else G
            print(f"  {W}{ep['endpoint']:<35}{RST} "
                  f"{ep['count']:>6}  "
                  f"{R if ep['errors'] else DIM}{ep['errors']:>5}{RST}  "
                  f"{rc}{rate_ep:>5.1f}%{RST}  "
                  f"{ep['avg_latency_ms']:>5}ms  "
                  f"{ep['max_latency_ms']:>5}ms")
        if len(endpoints) > 12:
            print(f"  {DIM}  … and {len(endpoints) - 12} more{RST}")
    print()


# ── Phase runner ──────────────────────────────────────────────────────────────

class PhaseResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.statuses: list[int] = []
        self.elapsed: float = 0.0
        self.errors: int = 0

    def rps(self) -> float:
        return len(self.statuses) / self.elapsed if self.elapsed else 0


async def run_phase(
    name: str,
    client: httpx.AsyncClient,
    requests: list[tuple[str, str]],  # [(method, url), ...]
    concurrency: int = 20,
    delay: float = 0.0,
    desc: str = "",
) -> PhaseResult:
    result = PhaseResult(name)
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(requests)

    print(f"\n{BOLD}{M}▶ Phase: {name}{RST}  {DIM}{desc}{RST}")
    print(f"  {DIM}{total} requests, concurrency={concurrency}{RST}")

    async def fetch(method: str, url: str) -> None:
        nonlocal done
        async with sem:
            try:
                if delay:
                    await asyncio.sleep(delay * random.uniform(0.5, 1.5))
                if method == "GET":
                    r = await client.get(url, timeout=8)
                else:
                    r = await client.request(method, url, timeout=8)
                result.statuses.append(r.status_code)
            except Exception:
                result.statuses.append(0)
                result.errors += 1
            done += 1
            if done % max(1, total // 10) == 0 or done == total:
                pct = done / total
                bar = _bar(done, total, 25, G if pct < 0.5 else Y if pct < 0.9 else G)
                print(f"\r  {bar} {pct*100:5.1f}%  {DIM}{done}/{total}{RST}", end="", flush=True)

    t0 = time.monotonic()
    await asyncio.gather(*[fetch(m, u) for m, u in requests])
    result.elapsed = time.monotonic() - t0
    print()  # newline after progress bar

    ok     = sum(1 for s in result.statuses if 200 <= s < 300)
    errors = sum(1 for s in result.statuses if s >= 400 or s == 0)
    print(f"  {G}2xx: {ok}{RST}  {R}4xx/5xx/err: {errors}{RST}  "
          f"{C}time: {result.elapsed:.2f}s{RST}  "
          f"{B}rps: {result.rps():.0f}{RST}")
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"\n{BOLD}{R}╔══════════════════════════════════════════════════════╗{RST}")
    print(f"{BOLD}{R}║   fastapi-flare  Bot / Stress Test Simulator          ║{RST}")
    print(f"{BOLD}{R}╚══════════════════════════════════════════════════════╝{RST}")
    print(f"  Target : {C}{BASE}{RST}")
    print(f"  Metrics: {C}{METRICS_URL}{RST}\n")

    limits = httpx.Limits(max_connections=300, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=BASE, limits=limits, follow_redirects=True) as client:

        # ── Warm-up connectivity ──────────────────────────────────────────
        try:
            r = await client.get("/", timeout=4)
            print(f"  {G}✓ App reachable — HTTP {r.status_code}{RST}")
        except Exception as e:
            print(f"  {R}✗ Cannot reach {BASE}: {e}{RST}")
            print(f"  {Y}Start the app first:  poetry run uvicorn examples.example:app --reload --port 8001{RST}\n")
            return

        await print_metrics(client, "baseline")

        # ── Phase 1: Normal traffic ───────────────────────────────────────
        normal = (
            [("GET", "/")]          * 30 +
            [("GET", "/items/1")]   * 20 +
            [("GET", "/items/50")]  * 20 +
            [("GET", "/items/99")]  * 20 +
            [("GET", "/docs")]      * 10
        )
        random.shuffle(normal)
        await run_phase(
            "Normal traffic",
            client, normal,
            concurrency=10, delay=0.03,
            desc="Legit requests to valid endpoints",
        )
        await print_metrics(client, "normal traffic")

        # ── Phase 2: URL enumeration (scanner / item ID farming) ──────────
        enum_urls = [("GET", f"/items/{i}") for i in range(1, 5001)]
        await run_phase(
            "URL enumeration — /items/1…5000",
            client, enum_urls,
            concurrency=50,
            desc="Simulates a scanner probing sequential IDs — tests route normalization + cap",
        )
        await print_metrics(client, "URL enumeration")

        # ── Phase 3: 404 probe burst (random paths) ───────────────────────
        def rnd_path() -> str:
            seg = lambda: "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 10)))
            depth = random.randint(1, 4)
            return "/" + "/".join(seg() for _ in range(depth))

        probes = [("GET", rnd_path()) for _ in range(2000)]
        await run_phase(
            "404 probe burst",
            client, probes,
            concurrency=80,
            desc="Random non-existent paths — all should collapse to <unmatched>",
        )
        await print_metrics(client, "404 probe burst")

        # ── Phase 4: Error flood (/boom) ──────────────────────────────────
        booms = [("GET", "/boom")] * 300
        await run_phase(
            "Error flood — /boom",
            client, booms,
            concurrency=30,
            desc="Repeated 500 errors — tests error logging throughput",
        )
        await print_metrics(client, "error flood")

        # ── Phase 5: Concurrent burst (concurrency safety) ────────────────
        burst_pool = [
            ("GET", "/"), ("GET", "/items/1"), ("GET", "/items/42"),
            ("GET", "/items/200"), ("GET", "/boom"),
        ]
        burst = [random.choice(burst_pool) for _ in range(500)]
        await run_phase(
            "Concurrent burst",
            client, burst,
            concurrency=200,
            desc="200 simultaneous goroutines — tests asyncio.Lock safety in FlareMetrics",
        )
        await print_metrics(client, "concurrent burst")

        # ── Phase 6: Dashboard API poll ───────────────────────────────────
        api_urls = (
            [("GET", "/flare/api/metrics")] * 20 +
            [("GET", "/flare/api/stats")]   * 10 +
            [("GET", "/flare/api/logs")]    * 10
        )
        await run_phase(
            "Dashboard API poll",
            client, api_urls,
            concurrency=10,
            desc="Dashboard endpoints should NOT appear in metrics (skiplist test)",
        )
        await print_metrics(client, "ALL PHASES COMPLETE")

    print(f"{BOLD}{G}✓ Stress test complete.{RST}  Open {C}http://localhost:8001/flare/metrics{RST} to inspect.\n")


if __name__ == "__main__":
    asyncio.run(main())
