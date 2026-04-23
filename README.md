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

## Log Entry Schema

Every captured error is stored as a structured `FlareLogEntry`:

```python
class FlareLogEntry(BaseModel):
    id: str                    # backend-native ID (row id for PG/SQLite)
    timestamp: datetime
    level: Literal["ERROR", "WARNING"]
    event: str                 # e.g. "http_exception", "unhandled_exception"
    message: str
    request_id: str | None     # UUID from X-Request-ID header
    endpoint: str | None
    http_method: str | None
    http_status: int | None
    ip_address: str | None
    duration_ms: int | None
    error: str | None
    stack_trace: str | None
    context: dict | None       # additional structured data
    request_body: dict | None  # captured request body (if enabled)
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