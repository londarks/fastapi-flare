<div align="center">

# ⚡ fastapi-flare

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

`fastapi-flare` tem suporte nativo para proteger o dashboard `/flare` via [Zitadel](https://zitadel.com/) OIDC.  
Existem dois modos de integração:

| Modo | Quando usar |
|---|---|
| **Browser (PKCE)** | Usuários acessam `/flare` pelo navegador — redirecionados para o login do Zitadel automaticamente |
| **Bearer Token** | Clientes de API enviam `Authorization: Bearer <token>` — sem redirecionamento |

> **Requer o extra `[auth]`:**
> ```bash
> pip install 'fastapi-flare[auth]'
> ```

### Pré-requisitos

No console do Zitadel:
1. Crie uma **Web Application** dentro de um projeto (tipo: PKCE)
2. Anote o **Domain** — ex: `auth.mycompany.com`
3. Anote o **Client ID** da aplicação
4. Anote o **Project ID** (visível nas configurações gerais do projeto)
5. **Para modo browser:** registre a URL de callback — ex: `https://myapp.com/flare/callback`

---

### Modo Browser (PKCE) — acesso pelo navegador

Quando `zitadel_redirect_uri` está configurado, abrir `/flare` no browser redireciona automaticamente para o Zitadel. Após o login, o Zitadel chama o callback, o token é salvo em cookie e o usuário é redirecionado de volta ao dashboard.

```python
setup(app, config=FlareConfig(
    redis_url="redis://localhost:6379",
    zitadel_domain="auth.mycompany.com",
    zitadel_client_id="000000000000000001",
    zitadel_project_id="000000000000000002",
    zitadel_redirect_uri="https://myapp.com/flare/callback",
))
```

Via variáveis de ambiente:

```bash
FLARE_ZITADEL_DOMAIN=auth.mycompany.com
FLARE_ZITADEL_CLIENT_ID=000000000000000001
FLARE_ZITADEL_PROJECT_ID=000000000000000002
FLARE_ZITADEL_REDIRECT_URI=https://myapp.com/flare/callback
FLARE_ZITADEL_SESSION_SECRET=<hex-de-32-bytes>  # python -c "import secrets; print(secrets.token_hex(32))"
```

**O que acontece:**
1. Usuário abre `https://myapp.com/flare` no browser
2. `fastapi-flare` detecta ausência de sessão → redireciona para `/flare/auth/login`
3. `/flare/auth/login` gera PKCE challenge, salva `code_verifier` + `state` na sessão, redireciona para o Zitadel
4. Usuário faz login na tela do Zitadel
5. Zitadel redireciona para `/flare/callback?code=...&state=...`
6. `fastapi-flare` valida o state, troca o code pelo `access_token`, chama `/oidc/v1/userinfo`
7. Dados do usuário e timestamp de expiração salvos na sessão (cookie assinado `flare_session`)
8. Usuário redirecionado para `/flare` — acesso liberado ✅

> **Importante:** registre exatamente `https://yourapp.com/flare/callback` como Redirect URI no app do Zitadel.

**Rotas criadas automaticamente:**

| Rota | O que faz |
|---|---|
| `GET /flare/auth/login` | Inicia o fluxo PKCE → redireciona ao Zitadel |
| `GET /flare/callback` | Recebe o code, troca por token, cria sessão |
| `GET /flare/auth/logout` | Limpa a sessão → redireciona ao login |

---

### Modo API (Bearer Token) — sem zitadel_redirect_uri

Quando `zitadel_redirect_uri` **não** está definido, a proteção valida o header `Authorization: Bearer <token>`. Ideal para quando o frontend já gerencia o fluxo PKCE e injeta o token nas requisições.

```python
setup(app, config=FlareConfig(
    redis_url="redis://localhost:6379",
    zitadel_domain="auth.mycompany.com",
    zitadel_client_id="000000000000000001",
    zitadel_project_id="000000000000000002",
    # sem zitadel_redirect_uri → modo Bearer
))
```

---

### Modo Manual — dependency customizada (avançado)

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

### Migração de projeto — aceitar tokens do projeto antigo

```bash
FLARE_ZITADEL_OLD_CLIENT_ID=old-client-id
FLARE_ZITADEL_OLD_PROJECT_ID=old-project-id
```

Tokens dos dois projetos são aceitos até você remover os campos `_old_*`.

---

### Como funciona internamente

- A sessão é gerenciada pelo `SessionMiddleware` do Starlette — cookie `flare_session` assinado com HMAC usando `zitadel_session_secret`. Todo o conteúdo (user, tokens, expiração) fica dentro do cookie — sem banco, sem Redis para gerenciar sessões.
- O `code_verifier` e `state` PKCE são armazenados na sessão (não em cookies separados), exatamente como recomendam as specs do PKCE.
- Após o callback, `fastapi-flare` chama `/oidc/v1/userinfo` para buscar os dados reais do usuário (email, nome, etc.) e os salva na sessão.
- Sessões expiram automaticamente após 1 hora — o usuário é redirecionado para login sem interrupção.
- Use `clear_jwks_cache()` para resetar o cache de JWKS em testes (modo Bearer).

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
