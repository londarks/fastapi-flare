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

    # ── Worker ────────────────────────────────────────────────────────
    worker_interval_seconds=5,
    worker_batch_size=100,
))
```

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

## Manual Logging

Push custom log entries from anywhere in your application:

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

MIT © [Gabriel](mailto:ondarks360@gmail.com)


**Lightweight self-hosted observability for FastAPI.**  
Backed by **Redis Streams** or **SQLite** — no SaaS, no overhead.

<br/>

[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Redis](https://img.shields.io/badge/Redis-Streams-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)

<br/>

<img src="assets/foto.png" alt="fastapi-flare dashboard preview" width="90%" style="border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.4);" />

</div>

---

## What is fastapi-flare?

`fastapi-flare` is a **self-hosted error tracking library** for FastAPI applications. It automatically captures HTTP and unhandled exceptions, stores them in Redis Streams, and exposes a beautiful dark-theme dashboard — all with a single line of code.

No external services. No configuration files. No noise.

---

## Features

| | |
|---|---|
| 🚀 **One-line setup** | `setup(app)` and you're done |
| 🔍 **Auto-capture** | HTTP 4xx/5xx and unhandled Python exceptions |
| 🖥️ **Admin dashboard** | Built-in at `/flare` — dark theme, filters, pagination |
| 🗄️ **Dual storage** | Redis Streams (production) or SQLite (zero-infra) |
| 🔥 **Fire-and-forget** | Logging never blocks or affects your request handlers |
| ⚙️ **Background worker** | Async task drains queue to stream every 5 seconds |
| 🕒 **Retention policies** | Time-based (default 7 days) + count-based (10k entries) |
| 🔐 **Auth-ready** | Protect the dashboard with any FastAPI `Depends()` |
| 🌍 **Env-configurable** | All settings available as `FLARE_*` environment variables |

---

## Installation

```bash
pip install fastapi-flare
```

All features (SQLite backend, Zitadel JWT auth) are included in the base install.

> **Requirements:** Python 3.11+, FastAPI. Redis is only required when using the default `redis` storage backend.

---

## Quick Start

**Redis** (default — production-ready, durable):

```python
from fastapi import FastAPI
from fastapi_flare import setup

app = FastAPI()
setup(app, redis_url="redis://localhost:6379")
```

**SQLite** (zero-infra — no Redis required):

```python
from fastapi import FastAPI
from fastapi_flare import FlareConfig, setup

app = FastAPI()
setup(app, config=FlareConfig(storage_backend="sqlite", sqlite_path="flare.db"))
```

Visit **`http://localhost:8000/flare`** to open the error dashboard.

---

## Storage Backends

### Redis (default)

Uses a Redis List as buffer queue and a Redis Stream as durable storage. Best for production deployments where Redis is already available.

```python
setup(app, config=FlareConfig(
    storage_backend="redis",           # default
    redis_url="redis://localhost:6379",
    redis_password=None,
    stream_key="flare:logs",
    queue_key="flare:queue",
))
```

**Docker:**
```bash
docker run -d -p 6379:6379 redis:7
```

### SQLite

Stores everything in a local `.db` file. No external services, no Docker, no configuration — ideal for local development, small deployments, or air-gapped environments.

```python
setup(app, config=FlareConfig(
    storage_backend="sqlite",
    sqlite_path="flare.db",            # path to the .db file
))
```

> The SQLite backend uses WAL mode and indexed queries for efficient reads and writes.

---

## Full Configuration

```python
from fastapi_flare import setup, FlareConfig

setup(app, config=FlareConfig(
    # --- Backend (choose one) ---
    storage_backend="redis",           # "redis" | "sqlite"

    # Redis options (storage_backend="redis")
    redis_url="redis://localhost:6379",
    redis_password=None,
    stream_key="flare:logs",
    queue_key="flare:queue",

    # SQLite options (storage_backend="sqlite")
    # sqlite_path="flare.db",

    # --- Shared ---
    max_entries=10_000,               # Count-based cap
    retention_hours=168,              # Time-based retention (7 days)

    # Dashboard
    dashboard_path="/flare",
    dashboard_title="My App — Errors",
    dashboard_auth_dependency=None,   # e.g. Depends(verify_token)

    # Worker
    worker_interval_seconds=5,
    worker_batch_size=100,
))
```

### Environment Variables

All options can be set via `FLARE_*` env vars — no code changes needed:

```bash
FLARE_REDIS_URL=redis://myhost:6379
FLARE_RETENTION_HOURS=72
FLARE_DASHBOARD_PATH=/errors
FLARE_DASHBOARD_TITLE="Production Errors"
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
| **Auto-refresh** | 30s polling toggle |

---

## Redis Data Model

`fastapi-flare` uses two Redis structures:

| Key | Type | Purpose |
|---|---|---|
| `flare:queue` | **List** | Incoming buffer — `LPUSH` by handlers, `RPOP` by worker |
| `flare:logs` | **Stream** | Durable time-ordered storage — `XADD` / `XREVRANGE` |

Stream entries are automatically trimmed by two policies applied on every worker cycle:

1. **Count-based** — `MAXLEN ~` keeps at most `max_entries` items
2. **Time-based** — `XTRIM MINID` removes entries older than `retention_hours`

---

## Log Entry Schema

Every captured error is stored as a structured `FlareLogEntry`:

```python
class FlareLogEntry(BaseModel):
    id: str                    # Redis Stream entry ID (millisecond-precise)
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
    context: dict | None       # Additional structured data
```

---

## Manual Logging

You can push custom log entries from anywhere in your application:

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
    redis_url="redis://localhost:6379",
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

> **Requires the `[auth]` extra:**
> ```bash
> pip install 'fastapi-flare[auth]'
> ```

### Prerequisites

In the Zitadel console:
1. Create a **Web Application** inside a project (type: PKCE / User Agent)
2. Note the **Domain** — e.g. `auth.mycompany.com`
3. Note the **Client ID** of the application
4. Note the **Project ID** (visible in the project's general settings)
5. **For browser mode:** register the callback URL — e.g. `https://myapp.com/flare/callback`

---

### Browser Mode (PKCE) — access from a browser

When `zitadel_redirect_uri` is configured, opening `/flare` in a browser automatically redirects to Zitadel. After login, Zitadel calls the callback, the session is created in a signed cookie, and the user is redirected back to the dashboard.

```python
setup(app, config=FlareConfig(
    redis_url="redis://localhost:6379",
    zitadel_domain="auth.mycompany.com",
    zitadel_client_id="000000000000000001",
    zitadel_project_id="000000000000000002",
    zitadel_redirect_uri="https://myapp.com/flare/callback",
))
```

Via environment variables:

```bash
FLARE_ZITADEL_DOMAIN=auth.mycompany.com
FLARE_ZITADEL_CLIENT_ID=000000000000000001
FLARE_ZITADEL_PROJECT_ID=000000000000000002
FLARE_ZITADEL_REDIRECT_URI=https://myapp.com/flare/callback
FLARE_ZITADEL_SESSION_SECRET=<32-byte-hex>  # python -c "import secrets; print(secrets.token_hex(32))"
```

**Flow:**
1. User opens `https://myapp.com/flare` in a browser
2. `fastapi-flare` detects no session → redirects to `/flare/auth/login`
3. `/flare/auth/login` generates a PKCE challenge, stores `code_verifier` + `state` in the session, redirects to Zitadel
4. User logs in on the Zitadel page
5. Zitadel redirects to the configured callback URL with `?code=...&state=...`
6. `fastapi-flare` validates the state, exchanges the code for an `access_token`, calls `/oidc/v1/userinfo`
7. User data and expiry timestamp are saved in the session (signed `flare_session` cookie)
8. User is redirected to `/flare` — access granted ✅

> **Important:** register exactly `https://yourapp.com/<callback-path>` as the Redirect URI in the Zitadel app.
> The callback path is extracted automatically from `zitadel_redirect_uri` — it does not need to be under `/flare`.

**Routes created automatically:**

| Route | Purpose |
|---|---|
| `GET /flare/auth/login` | Starts the PKCE flow → redirects to Zitadel |
| `GET <callback-path>` | Receives the code, exchanges it for a token, creates the session |
| `GET /flare/auth/logout` | Clears the session → redirects to login |

---

### API Mode (Bearer Token) — no zitadel_redirect_uri

When `zitadel_redirect_uri` is **not** set, the dashboard validates the `Authorization: Bearer <token>` header. Ideal when the frontend already manages the PKCE flow and injects the token into requests.

```python
setup(app, config=FlareConfig(
    redis_url="redis://localhost:6379",
    zitadel_domain="auth.mycompany.com",
    zitadel_client_id="000000000000000001",
    zitadel_project_id="000000000000000002",
    # no zitadel_redirect_uri → Bearer mode
))
```

---

### Manual Mode — custom dependency (advanced)

```python
from fastapi_flare import setup, FlareConfig
from fastapi_flare.zitadel import make_zitadel_dependency

dep = make_zitadel_dependency(
    domain="auth.mycompany.com",
    client_id="000000000000000001",
    project_id="000000000000000002",
)

setup(app, config=FlareConfig(
    redis_url="redis://localhost:6379",
    dashboard_auth_dependency=dep,
))
```

---

### Project migration — accepting tokens from the old project

```bash
FLARE_ZITADEL_OLD_CLIENT_ID=old-client-id
FLARE_ZITADEL_OLD_PROJECT_ID=old-project-id
```

Tokens from both projects are accepted until you remove the `_old_*` fields.

---

### How it works internally

- The session is managed by Starlette's `SessionMiddleware` — a `flare_session` cookie signed with HMAC using `zitadel_session_secret`. All content (user, token, expiry) lives inside the cookie — no database or Redis required for session management.
- The PKCE `code_verifier` and `state` are stored in the session (not in separate cookies), following the PKCE spec recommendations.
- After the callback, `fastapi-flare` calls `/oidc/v1/userinfo` to fetch the real user data (email, name, etc.) and saves it in the session.
- Sessions expire automatically after 1 hour — the user is transparently redirected to login.
- Use `clear_jwks_cache()` to reset the JWKS cache in tests (Bearer mode).

---

## Running the Example

**SQLite mode** (no dependencies):

```bash
poetry run uvicorn examples.example:app --reload --port 8000
```

**Redis mode:**

```bash
# 1. Copy and configure environment
cp .env.example .env

# 2. Start Redis (Docker)
docker run -d -p 6379:6379 redis:7

# 3. Switch the example to Redis and run
# In examples/example.py, change:
#   FlareConfig(storage_backend="redis")  # and set FLARE_REDIS_URL in .env
poetry run uvicorn examples.example:app --reload --port 8000
```

**Test routes:**

| Route | Behavior |
|---|---|
| `GET /` | Returns 200 OK |
| `GET /boom` | Triggers `RuntimeError` → captured as ERROR |
| `GET /items/999` | Triggers `HTTPException 404` → captured as WARNING |
| `GET /flare` | Opens the error dashboard |

---

## Comparison

| Project | What it does |
|---|---|
| `sentry-sdk` | Full error tracking SaaS — more features, external dependency |
| `fastapi-analytics` | Endpoint analytics / performance — not error-focused |
| `fastapi-middleware-logger` | HTTP logging only, no storage or dashboard |
| `api-watch` | Real-time monitoring, Flask/FastAPI |
| **`fastapi-flare`** | **Self-hosted, zero-config error visibility — no external services** |

`fastapi-flare` is for teams that want **local, observable, production-ready error tracking** without the overhead of a full observability platform.

---

## Why not Sentry?

Sentry is a great product — but it comes with trade-offs that not every team wants to accept.

| | fastapi-flare | Sentry |
|---|---|---|
| **Hosting** | Self-hosted, your infra | External SaaS |
| **Account required** | No | Yes |
| **Infrastructure** | Redis only | Kafka, ClickHouse, Postgres, … |
| **Cost** | Zero | Free tier → paid plans |
| **Privacy** | Data never leaves your server | Data sent to third-party |
| **Setup** | One `setup(app)` call | SDK + DSN + account config |
| **Customization** | Full source access | Configuration only |

`fastapi-flare` is the right choice when you need **fast, private, zero-dependency error visibility** — especially in self-hosted, air-gapped, or cost-sensitive environments.  
For large-scale teams who need release tracking, performance monitoring, and team workflows, Sentry remains the better fit.

---

## License

MIT © [Gabriel](mailto:ondarks360@gmail.com)
