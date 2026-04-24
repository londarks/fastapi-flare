<div align="center">

# ⚡ fastapi-flare

**Lightweight self-hosted debugger and metrics dashboard for FastAPI.**  
Zero-config by default (SQLite) — PostgreSQL-ready for production.

<br/>

[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-asyncpg-336791?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)

<br/>

<img src="assets/foto.png" alt="fastapi-flare dashboard preview" width="90%" style="border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.4);" />

</div>

---

## What is fastapi-flare?

`fastapi-flare` is a **self-hosted error tracking and metrics library** for FastAPI applications.  
It automatically captures HTTP and unhandled exceptions, stores them locally or in PostgreSQL, and exposes a dark-theme dashboard — all with a single line of code.

No external services. No SaaS. No noise.

---

## Features

| | |
|---|---|
| 🚀 **One-line setup** | `setup(app)` — works immediately, no config required |
| 🔍 **Auto-capture** | HTTP 4xx/5xx and unhandled Python exceptions |
| 🧵 **Non-HTTP capture** | Background tasks, workers, `logger.exception()`, stray asyncio tasks |
| 🧩 **Issue grouping** | Deterministic fingerprint collapses repeated errors into one issue with occurrence count, first/last seen, resolve/reopen |
| 🖥️ **Admin dashboard** | Built-in at `/flare` — dark theme, filters, pagination |
| 🗄️ **Dual storage** | SQLite (zero-config default) or PostgreSQL (production) |
| 🔥 **Fire-and-forget** | Logging never blocks your request handlers |
| ⚙️ **Background worker** | Async task runs retention cleanup every 5 seconds |
| 🕒 **Retention policies** | Time-based (default 7 days) + count-based (10k entries) |
| 🔐 **Auth-ready** | Protect the dashboard with any FastAPI `Depends()` |
| 🌍 **Env-configurable** | All settings available via `FLARE_*` environment variables |

---

## Installation

```bash
pip install fastapi-flare
```

> **Requirements:** Python 3.11+, FastAPI.  
> `aiosqlite` and `asyncpg` are bundled — no extra installs needed for either backend.

---

## Quick Start

**Zero-config** (SQLite, works immediately):

```python
from fastapi import FastAPI
from fastapi_flare import setup

app = FastAPI()
setup(app)
# Dashboard at http://localhost:8000/flare
# Creates flare.db automatically — no setup required.
```

**PostgreSQL** (production):

```python
from fastapi_flare import setup, FlareConfig

setup(app, config=FlareConfig(
    storage_backend="postgresql",
    pg_dsn="postgresql://user:password@localhost:5432/mydb",
))
```

---

## Storage Backends

### SQLite (default)

Zero-config local file storage. Works immediately without any external dependencies.  
Ideal for development, quick testing, small deployments, and air-gapped environments.

```python
setup(app, config=FlareConfig(
    storage_backend="sqlite",     # default — can be omitted
    sqlite_path="flare.db",       # path to the .db file
))
```

Via environment variables:
```bash
FLARE_STORAGE_BACKEND=sqlite
FLARE_SQLITE_PATH=/data/flare.db
```

> Uses WAL mode and indexed queries for efficient reads and writes.

---

### PostgreSQL (production)

Production-grade backend using `asyncpg` with a connection pool.  
Direct INSERT on every log entry — no intermediate buffer or drain step.

```python
setup(app, config=FlareConfig(
    storage_backend="postgresql",
    pg_dsn="postgresql://user:password@localhost:5432/mydb",
))
```

Via environment variables:
```bash
FLARE_STORAGE_BACKEND=postgresql
FLARE_PG_DSN=postgresql://user:password@localhost:5432/mydb
```

> **Special characters in passwords:**  
> URL-encode `@` as `%40`, `#` as `%23`, `&` as `%26`, etc.  
> Example: `password@123` → `FLARE_PG_DSN=postgresql://user:password%40123@host:5432/db`

The table `flare_logs` (or your custom name) is created automatically on first connection.

---

## Multi-Project Isolation

You can run multiple independent APIs storing their logs in the same PostgreSQL server.  
Two isolation strategies are available — choose what fits best:

### Strategy 1 — One database per project (full isolation)

Each API points to a different database. Complete separation at the database level.

```bash
# API checkout
FLARE_PG_DSN=postgresql://user:pass@host:5432/checkout_db

# API auth
FLARE_PG_DSN=postgresql://user:pass@host:5432/auth_db

# API orders
FLARE_PG_DSN=postgresql://user:pass@host:5432/orders_db
```

### Strategy 2 — One database, separate tables (centralized)

All APIs share one database, each writing to its own table.  
Simpler to manage — one database to back up, one server to monitor.

```bash
# All APIs point to the same database
FLARE_PG_DSN=postgresql://user:pass@host:5432/mydb

# Each project gets its own table
FLARE_PG_TABLE_NAME=flare_logs_checkout  # API checkout
FLARE_PG_TABLE_NAME=flare_logs_auth      # API auth
FLARE_PG_TABLE_NAME=flare_logs_orders    # API orders
```

Each table is created automatically by `flare` on first connection.

---

## Full Configuration

```python
from fastapi_flare import setup, FlareConfig

setup(app, config=FlareConfig(
    # ── Storage (choose one) ──────────────────────────────────────────
    storage_backend="sqlite",          # "sqlite" (default) | "postgresql"

    # SQLite options
    sqlite_path="flare.db",

    # PostgreSQL options
    pg_dsn="postgresql://user:pass@localhost:5432/mydb",
    pg_table_name="flare_logs",        # custom table name for multi-project setups

    # ── Retention ─────────────────────────────────────────────────────
    max_entries=10_000,                # count-based cap
    retention_hours=168,               # time-based retention (7 days)

    # ── Dashboard ─────────────────────────────────────────────────────
    dashboard_path="/flare",
    dashboard_title="My App — Errors",
    dashboard_auth_dependency=None,    # e.g. Depends(verify_token)

    # ── Request tracking (HTTP Requests tab) ───────────────────────────
    track_requests=True,           # enable the HTTP Requests tab (default: True)
    track_2xx_requests=False,      # also record successful 2xx responses (default: False)
    request_max_entries=1000,      # ring buffer size for tracked requests
    capture_request_headers=False, # store request headers per entry (adds data volume)

    # ── Non-HTTP error capture ─────────────────────────────────────────
    capture_logging=False,             # forward WARNING+ from logging module
    capture_logging_loggers=None,      # "myapp.worker,myapp.jobs" — None = root
    capture_asyncio_errors=False,      # capture stray asyncio task failures

    # ── Worker ────────────────────────────────────────────────────────
    worker_interval_seconds=5,
    worker_batch_size=100,
))
```

> **Tip — showing all requests in the HTTP Requests tab:**  
> By default only 4xx and 5xx are recorded. To also capture 200 OK and other successful
> responses, set `track_2xx_requests=True` (or `FLARE_TRACK_2XX_REQUESTS=true`).

### Environment Variables

All options can be configured via `FLARE_*` environment variables — no code changes needed:

```bash
FLARE_STORAGE_BACKEND=postgresql
FLARE_PG_DSN=postgresql://user:pass@localhost:5432/mydb
FLARE_PG_TABLE_NAME=flare_logs
FLARE_RETENTION_HOURS=72
FLARE_MAX_ENTRIES=5000
FLARE_DASHBOARD_PATH=/errors
FLARE_DASHBOARD_TITLE="Production Errors"

# Request tracking
FLARE_TRACK_REQUESTS=true
FLARE_TRACK_2XX_REQUESTS=true   # record 200 OK and other successful responses
FLARE_REQUEST_MAX_ENTRIES=1000
FLARE_CAPTURE_REQUEST_HEADERS=false

# Non-HTTP error capture
FLARE_CAPTURE_LOGGING=true                 # forward logger.exception / logger.error
FLARE_CAPTURE_LOGGING_LOGGERS=myapp.worker # optional comma-separated allow-list
FLARE_CAPTURE_ASYNCIO_ERRORS=true          # capture stray asyncio tasks
```

---

## Dashboard

The built-in dashboard gives you full visibility into your application errors without leaving your infrastructure.

| Feature | Detail |
|---|---|
| **URL** | `{dashboard_path}` (default `/flare`) |
| **Stats cards** | Errors/Warnings in last 24h, total entries, latest error time |
| **Filters** | Level (ERROR / WARNING), event name, full-text search |
| **Table** | Timestamp, level badge, event, message, endpoint, HTTP status |
| **Detail modal** | Full message, error, stack trace, request metadata, context JSON |
| **Storage overview** | Backend info, connection status, pool stats (PostgreSQL) or file size (SQLite) |
| **Auto-refresh** | 30s polling toggle |

---

## Issue Grouping *(new in 0.3.0)*

Until 0.2.x every captured error was a standalone row. Five hundred occurrences
of the same `ValueError` meant five hundred lines to read. Starting in 0.3.0
`fastapi-flare` groups semantically-equivalent errors into **issues** the way
Sentry and Rollbar do — with occurrence counting, first/last-seen, and a
resolve/reopen workflow.

### What changed

- New aba **Issues** at `/flare/issues` — grouped view.
- New table `flare_issues` (auto-migrated on both SQLite and PostgreSQL).
- New column `issue_fingerprint` on `flare_logs` (idempotent `ADD COLUMN IF NOT EXISTS`).
- Every `push_log()` now computes a deterministic fingerprint and upserts the issue.
- New JSON API under `/flare/api/issues`.
- The existing Errors tab (`/flare`) is unchanged — it remains the raw stream.

**No configuration required.** Upgrading is transparent; existing deployments
get the new table and column on next start, and rows created before the upgrade
simply have `issue_fingerprint = NULL`.

### How the fingerprint works

For each captured log, a 16-char `blake2b` hash is computed from:

| Scenario | Hash input |
|---|---|
| Has stack trace (generic exception / `logger.exception`) | `exception_type \| endpoint \| top-5 frames` |
| HTTP-only signal (raised `HTTPException`) | `http \| status_code \| endpoint` |
| Validation error (`RequestValidationError`) | `validation \| endpoint` |
| Fallback | `event \| endpoint \| message[:200]` |

Each stack frame is normalised to `(basename(file), function_name)` — **line
numbers and absolute paths are dropped on purpose** so ordinary refactors
(moving code down the file, renaming a `/srv/app/...` path) do not re-fingerprint
the same bug. Rename the file or the function and the issue does split — that's
usually the signal you want.

This is intentionally different from the cooldown fingerprint in
`fastapi_flare.alerting`, which stays coarse (`event + endpoint`) to throttle
Slack/Discord pings.

### Behaviour you can rely on

- **500 identical errors → 1 issue** with `occurrence_count = 500`, the latest
  `last_seen`, and the original `first_seen` preserved.
- **Same `ValueError`, different call path → separate issues.** Lets you tell
  apart an actual regression from an unrelated code path that happens to raise
  the same type.
- **Resolved issues reopen automatically** the next time the same fingerprint
  fires — `resolved` flips back to `false` and `resolved_at` is cleared.
- **Level upgrades, never downgrades.** A WARNING issue that later hits ERROR
  becomes ERROR for good.
- **Issue state survives retention.** When `retention_hours` purges raw logs,
  the `flare_issues` row keeps `occurrence_count`, `first_seen`, and
  `last_seen`. Only the drill-down list of occurrences shrinks.

### Using the dashboard

Open `/flare/issues`:

- **Stat cards** at the top — Open / New (24h) / Resolved (7d) / Total.
- **Filter chips** — All / Open / Resolved.
- **Search** — matches on `exception_type`, `endpoint`, or sample message.
- **Row click** — opens an issue modal with the list of occurrences. Each
  occurrence row is clickable and shows the full stack trace, request body,
  headers, and context, the same as the Errors tab.
- **Resolve / Reopen** button inside the modal. The change is reflected
  immediately in the stat cards and list.

### JSON API

| Method | Path | Description |
|---|---|---|
| `GET`   | `/flare/api/issues`                 | Paginated list. Query: `page`, `limit`, `resolved=true/false`, `search=...` |
| `GET`   | `/flare/api/issues/stats`           | Counts used by the stat cards |
| `GET`   | `/flare/api/issues/{fingerprint}`   | Issue detail + paginated occurrences |
| `PATCH` | `/flare/api/issues/{fingerprint}`   | Body `{"resolved": true \| false}` |

Example:

```bash
curl -s http://localhost:8003/flare/api/issues?resolved=false | jq
# { "issues": [ {...} ], "total": 12, "page": 1, "limit": 50, "pages": 1 }

curl -X PATCH http://localhost:8003/flare/api/issues/63d05fb9d296a8e1 \
  -H 'Content-Type: application/json' \
  -d '{"resolved": true}'
# { "ok": true, "action": "issue_status", "detail": "Issue resolved" }
```

Full reference (fingerprint internals, storage model, migration, gotchas):
[`docs/issues.md`](docs/issues.md).

### Try it — the demo app

A dedicated example under `examples/example_issues.py` exercises every
grouping scenario:

```bash
poetry run uvicorn examples.example_issues:app --reload --port 8003
```

| Route | What it proves |
|---|---|
| `GET /boom/value-error` (×10) | 1 issue with `occurrence_count = 10` |
| `GET /boom/key-error`   (×5)  | separate issue (different exception type) |
| `GET /boom/deep`        (×3)  | separate issue (same `ValueError`, different stack) |
| `GET /items/{iid}`            | 404 → `HTTP 404` issue per endpoint |
| `GET /users`                  | 403 → `HTTP 403` issue |
| `POST /signup` with bad body  | 422 → `RequestValidationError` issue per endpoint |
| `POST /orders` with bad total | 500 → `RuntimeError` issue |
| `GET /trigger/manual`         | captured via `capture_exception()` outside the request path |
| `GET /stress/{n}`             | generates *n* errors at once to exercise pagination |

Full validation walkthrough:

```bash
# 1. Start the app
poetry run uvicorn examples.example_issues:app --reload --port 8003

# 2. Fire traffic
for i in {1..10}; do curl -s http://localhost:8003/boom/value-error >/dev/null; done
for i in {1..5};  do curl -s http://localhost:8003/boom/key-error   >/dev/null; done
curl -s http://localhost:8003/items/999 >/dev/null
curl -s http://localhost:8003/stress/50 >/dev/null

# 3. Open /flare/issues — you'll see one row per issue kind with the right counts.
# 4. Click a row → modal shows every occurrence. Click one → full stack trace.
# 5. Click Resolve → issue leaves the Open filter.
# 6. Fire the same endpoint again → issue reopens automatically.
```

### What's stored — `FlareIssue`

```python
class FlareIssue(BaseModel):
    fingerprint: str            # 16-char blake2b hex, primary key
    exception_type: str | None  # "ValueError" | "HTTP 404" | "RequestValidationError" | ...
    endpoint: str | None
    sample_message: str         # first message seen for this issue
    sample_request_id: str | None
    occurrence_count: int
    first_seen: datetime
    last_seen: datetime
    level: Literal["ERROR", "WARNING"]  # upgrades to ERROR, never downgrades
    resolved: bool
    resolved_at: datetime | None
```

### Known limitations

- **Dynamic path params** — endpoints like `/items/1` and `/items/2` currently
  produce separate issues because handlers still capture `request.url.path`
  (raw). A follow-up will use the matched route template (`/items/{iid}`)
  instead, collapsing those into a single issue.
- **SQLite multi-tenancy** — `flare_issues` uses a fixed name on SQLite
  (matches the rest of the SQLite backend, which already has fixed `logs`,
  `requests`, `flare_settings`, `flare_metrics_snapshots`). PostgreSQL
  derives the issues table from `pg_table_name` like everything else, so
  multi-project PG setups work out of the box.
- **No backfill** — logs captured before 0.3.0 have `NULL` for
  `issue_fingerprint` and are not surfaced on the Issues tab. Running traffic
  after upgrade populates the grouping from that point forward.

---

## Log Entry Schema

Every captured error is stored as a structured `FlareLogEntry`:

```python
class FlareLogEntry(BaseModel):
    id: str                       # backend-native ID (row id for PG/SQLite)
    timestamp: datetime
    level: Literal["ERROR", "WARNING"]
    event: str                    # e.g. "http_exception", "unhandled_exception"
    message: str
    request_id: str | None        # UUID from X-Request-ID header
    issue_fingerprint: str | None # links this row to a FlareIssue (v0.3.0+)
    endpoint: str | None
    http_method: str | None
    http_status: int | None
    ip_address: str | None
    duration_ms: int | None
    error: str | None
    stack_trace: str | None
    context: dict | None          # additional structured data
    request_body: dict | None     # captured request body (if enabled)
```

---

## Capturing Non-HTTP Errors

By default `fastapi-flare` captures HTTP 4xx/5xx and unhandled exceptions
inside the request path. You can also route **errors that happen outside
any request** — background tasks, workers, cron jobs, consumers, startup
code, detached asyncio tasks — into the same dashboard.

Three mechanisms are available. Use any combination.

### 1. Python `logging` integration (automatic)

Forwards every `WARNING` / `ERROR` record from Python's standard logging
into Flare. Zero changes to your existing code — any `logger.exception(...)`
or `logger.error(...)` already in your codebase starts showing up on `/flare`.

```python
setup(app, config=FlareConfig(
    capture_logging=True,
    # Optional: only listen to specific loggers.
    # Empty / omitted = attach to the root logger (catches everything that propagates).
    capture_logging_loggers="myapp.worker,myapp.jobs",
))
```

Anywhere in your code:

```python
import logging
logger = logging.getLogger("myapp.worker")

try:
    process_job()
except Exception:
    logger.exception("job failed")   # ← appears in /flare
```

The captured entry gets `event=log.<logger-name>`, `endpoint=None`, and a
`context` auto-populated with `logger`, `module`, `func`, `line`, `file`.

### 2. Manual capture (explicit)

When you've already caught an exception and want to record it without
re-raising, call `capture_exception`:

```python
from fastapi_flare import capture_exception

try:
    await charge_customer(order)
except StripeError as e:
    await capture_exception(
        e,
        event="payment.retry_exhausted",
        context={"order_id": order.id, "attempts": 5},
    )
    # handle gracefully — user is not affected
```

For non-exception signals (rate limits, degraded deps, audit events):

```python
from fastapi_flare import capture_message

await capture_message(
    "rate-limit hit on outbound API",
    level="WARNING",
    event="outbound.rate_limited",
    context={"api": "sendgrid", "hits": 142},
)
```

### 3. asyncio unhandled-task capture

`asyncio.create_task(...)` that raises without being awaited normally
**disappears silently** — the event loop just prints a warning to stderr.
Enable capture so these land on the dashboard:

```python
setup(app, config=FlareConfig(
    capture_asyncio_errors=True,
))
```

Now any detached task that blows up gets recorded with
`event=asyncio.unhandled`, full stack trace, and a `context` describing
the task.

### `context` — free-form metadata

The `context` dict you pass (or the one auto-filled by the logging handler)
is stored as JSON and rendered under the **Context** section of the modal.
Use it for anything that doesn't fit the fixed fields — job IDs, provider
names, feature flags, versions, etc. Keys matching
`FlareConfig.sensitive_fields` (`password`, `token`, `api_key`, …) are
automatically redacted before storage.

### Low-level API

All three mechanisms ultimately call `push_log`, which is still public
if you need full control:

```python
from fastapi_flare.queue import push_log

await push_log(
    config,
    level="ERROR",
    event="payment_failed",
    message="Stripe charge declined",
    context={"order_id": "ord_123", "amount": 2500},
)
```

---

## Protecting the Dashboard

Secure the dashboard using any FastAPI dependency:

```python
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer

bearer = HTTPBearer()

def verify_token(token=Security(bearer)):
    if token.credentials != "my-secret":
        raise HTTPException(status_code=401, detail="Unauthorized")

setup(app, config=FlareConfig(
    dashboard_auth_dependency=verify_token,
))
```

---

## Zitadel Authentication

`fastapi-flare` has built-in support for protecting the `/flare` dashboard via [Zitadel](https://zitadel.com/) OIDC.  
Two integration modes are available:

| Mode | When to use |
|---|---|
| **Browser (PKCE)** | Users access `/flare` from a browser — automatically redirected to the Zitadel login page |
| **Bearer Token** | API clients send `Authorization: Bearer <token>` — no redirect |

### Prerequisites

In the Zitadel console:
1. Create a **Web Application** inside a project (type: PKCE / User Agent)
2. Note the **Domain** — e.g. `auth.mycompany.com`
3. Note the **Client ID** of the application
4. Note the **Project ID** (visible in the project's general settings)
5. **For browser mode:** register the callback URL — e.g. `https://myapp.com/flare/callback`

### Browser Mode (PKCE)

```python
setup(app, config=FlareConfig(
    zitadel_domain="auth.mycompany.com",
    zitadel_client_id="000000000000000001",
    zitadel_project_id="000000000000000002",
    zitadel_redirect_uri="https://myapp.com/flare/callback",
    zitadel_session_secret="<32-byte-hex>",
))
```

Via environment variables:
```bash
FLARE_ZITADEL_DOMAIN=auth.mycompany.com
FLARE_ZITADEL_CLIENT_ID=000000000000000001
FLARE_ZITADEL_PROJECT_ID=000000000000000002
FLARE_ZITADEL_REDIRECT_URI=https://myapp.com/flare/callback
FLARE_ZITADEL_SESSION_SECRET=<32-byte-hex>
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
```

**Flow:**
1. User opens `/flare` → no session → redirected to `/flare/auth/login`
2. PKCE challenge generated → redirected to Zitadel login
3. User logs in → Zitadel redirects to `callback-url?code=...`
4. `fastapi-flare` exchanges code for token → creates signed session cookie
5. User redirected to `/flare` — access granted ✅

**Routes created automatically:**

| Route | Purpose |
|---|---|
| `GET /flare/auth/login` | Starts the PKCE flow → redirects to Zitadel |
| `GET <callback-path>` | Receives the code, exchanges it, creates the session |
| `GET /flare/auth/logout` | Clears the session → redirects to login |

### API Mode (Bearer Token)

When `zitadel_redirect_uri` is **not** set, the dashboard validates the `Authorization: Bearer <token>` header directly. No redirect flow.

### Manual Mode (custom dependency)

```python
from fastapi_flare.zitadel import make_zitadel_dependency

dep = make_zitadel_dependency(
    domain="auth.mycompany.com",
    client_id="000000000000000001",
    project_id="000000000000000002",
)
setup(app, config=FlareConfig(dashboard_auth_dependency=dep))
```

---

## Running the Example

```bash
# Zero-config SQLite (no setup needed)
poetry run uvicorn examples.example:app --reload --port 8000
# Dashboard at http://localhost:8000/flare
```

**PostgreSQL example** — set in your `.env`:
```bash
FLARE_STORAGE_BACKEND=postgresql
FLARE_PG_DSN=postgresql://user:pass@localhost:5432/mydb
```

**Test routes:**

| Route | Behavior |
|---|---|
| `GET /` | Returns 200 OK |
| `GET /boom` | Triggers `RuntimeError` → captured as ERROR |
| `GET /items/999` | Triggers `HTTPException 404` → captured as WARNING |
| `GET /flare` | Opens the error dashboard |

### Non-HTTP capture demo

A dedicated example exercises the `capture_logging`,
`capture_asyncio_errors`, `capture_exception`, and `capture_message`
features end-to-end:

```bash
poetry run uvicorn examples.example_non_http_capture:app --reload --port 8002
# Dashboard at http://localhost:8002/flare
```

| Route | What it captures |
|---|---|
| `GET /trigger/logger` | `logger.exception(...)` inside a handler |
| `GET /trigger/manual` | `capture_exception(e, context=...)` |
| `GET /trigger/asyncio` | A stray `asyncio.create_task` that raises |
| `GET /trigger/background` | A worker thread that logs via `logger.exception` |
| `GET /trigger/warn` | `capture_message(...)` at WARNING level |

The app also emits two entries on **startup** — proving that records
outside the request path are visible on `/flare` before anyone hits
an endpoint.

---

## Comparison

| Project | What it does |
|---|---|
| `sentry-sdk` | Full error tracking SaaS — more features, external dependency |
| `fastapi-analytics` | Endpoint analytics / performance — not error-focused |
| `fastapi-middleware-logger` | HTTP logging only, no storage or dashboard |
| **`fastapi-flare`** | **Self-hosted, zero-config error tracking — SQLite or PostgreSQL** |

---

## Why not Sentry?

| | fastapi-flare | Sentry |
|---|---|---|
| **Hosting** | Self-hosted, your infra | External SaaS |
| **Account required** | No | Yes |
| **Setup** | One `setup(app)` call | SDK + DSN + account config |
| **Storage** | SQLite or PostgreSQL | Kafka, ClickHouse, Postgres, … |
| **Cost** | Zero | Free tier → paid plans |
| **Privacy** | Data never leaves your server | Data sent to third-party |
| **Customization** | Full source access | Configuration only |

`fastapi-flare` is the right choice when you need **fast, private, zero-dependency error visibility** — especially in self-hosted, air-gapped, or cost-sensitive environments.

---

## License

MIT © [Gabriel](mailto:contato@londarks.com)