# API Stability — what's stable, what isn't

> **Honest framing.** `fastapi-flare` is pre-1.0. The API isn't frozen yet, and
> minor versions (0.x → 0.y) may include behaviour changes. This document spells
> out exactly which surfaces are considered "public API" today and which are
> "internal" — so when you read a CHANGELOG entry you know whether you have
> breakage to investigate or not.

## Versioning today

| Bump | Rule | Example |
|---|---|---|
| **Patch** (`0.4.x`) | No new features. Bug fixes, docs, internal refactors that preserve behaviour. | `0.4.0` → `0.4.1` |
| **Minor** (`0.x.0`) | New optional features. Behaviour of existing config may shift slightly if it improves correctness. | `0.3.x` → `0.4.0` |
| **Major** (`x.0.0`) | Will commit to strict semver once the surface stabilises (target: after issue rules + alert rules ship). | `0.x` → `1.0.0` |

**Practical recommendation while pre-1.0**: pin **exact** in production:

```toml
# pyproject.toml
fastapi-flare = "0.4.0"   # NOT "^0.4.0"
```

Read the `CHANGELOG.md` before bumping. Patches are safe to take blindly;
minors deserve a quick scan.

## What's "public" today

Stable enough that breaking changes will go in a major bump (or, if absolutely
necessary, will be flagged loudly in the CHANGELOG with at least one minor of
deprecation).

### Setup surface
- `from fastapi_flare import setup, FlareConfig` — the call signature of
  `setup(app, config=...)` and the `FlareConfig` field names.
- All `FLARE_*` environment variables documented in the README.

### Public functions
- `capture_exception(exc, *, config=None, event=..., context=...)`
- `capture_message(msg, *, level=..., config=None, event=..., context=...)`
- `install_logging_capture` / `uninstall_logging_capture`
- `install_asyncio_capture`
- Notifier classes: `WebhookNotifier`, `SlackNotifier`, `DiscordNotifier`, `TeamsNotifier`
- Zitadel helpers: `make_zitadel_dependency`, `make_zitadel_browser_dependency`,
  `exchange_zitadel_code`, `verify_zitadel_token`, `clear_jwks_cache`,
  `ZitadelBrowserRedirect`

### Public schemas (Pydantic models)
- `FlareLogEntry`, `FlareLogPage`, `FlareStats`
- `FlareIssue`, `FlareIssuePage`, `FlareIssueDetail`, `FlareIssueStats`
- `FlareRequestEntry`, `FlareRequestPage`, `FlareRequestStats`
- `FlareMetricsSnapshot`, `FlareEndpointMetric`
- `FlareHealthReport`, `FlareStorageOverview`, `FlareStorageActionResult`

### Public REST API
The endpoints under `{dashboard_path}/api/...` are stable in **shape** (their
JSON schemas correspond to the Pydantic models above). New optional fields
may be added in minor versions; existing fields are not removed or renamed
without a major bump.

| Endpoint | Stability |
|---|---|
| `GET /flare/api/logs` | Stable |
| `GET /flare/api/stats` | Stable |
| `GET /flare/api/requests` | Stable |
| `GET /flare/api/request-stats` | Stable |
| `GET /flare/api/metrics` | Stable |
| `GET /flare/api/issues`, `/api/issues/stats`, `/api/issues/{fp}`, `PATCH /api/issues/{fp}` | Stable since 0.3.0 |
| `GET /flare/health` | Stable, **public** (no auth) |
| `GET /flare/api/storage/overview`, `POST /flare/api/storage/trim`, `POST /flare/api/storage/clear` | Stable |
| `GET /flare/api/settings`, `POST /flare/api/settings`, `POST /flare/api/notifications/test` | Stable |

### Storage table names
- `flare_logs` (PG) / `logs` (SQLite)
- `flare_requests` (PG) / `requests` (SQLite)
- `flare_issues` (PG + SQLite)
- `flare_settings` (PG + SQLite)
- `flare_metrics_snapshots` (PG + SQLite)

These names are part of the contract — column additions are non-breaking
(via `ADD COLUMN IF NOT EXISTS` / `ALTER TABLE` try-except), but renames or
drops would be a major bump.

## What's "internal"

These can change without warning between minor versions. Don't import them
from your application code.

- `fastapi_flare.queue.push_log` — public **for now** because non-HTTP capture
  uses it, but the signature may add new keyword arguments freely. Use
  `capture_exception` / `capture_message` instead when possible.
- `fastapi_flare.fingerprint.compute_fingerprint` — the hashing scheme is an
  implementation detail. Two minor versions could produce different
  fingerprints for the same exception (which would re-create issues). The
  CHANGELOG will call this out if it changes; don't depend on the hex output
  being stable across versions.
- `fastapi_flare.middleware.*` — the middleware classes themselves are public
  (they need to be `add_middleware`-able). Their internal helpers
  (`_extract_request_body`, `_drain_and_rebuild`, `_SCOPE_BODY_KEY` constant)
  are internal.
- `fastapi_flare.storage.base.FlareStorageProtocol` — public, but minor
  versions may add **new** abstract methods. Custom backends should expect
  to implement new methods on minor bumps.
- `fastapi_flare.handlers.*` — internal. Don't import from there.
- `fastapi_flare.alerting.*` — internal scheduler.
- Anything starting with `_` — internal by convention.

## Behaviour-change history (pre-1.0)

When something existing changes its observable behaviour without a config
flag, it goes here. Production users should treat each entry as a checkpoint
before upgrading.

| Version | Change | Migration |
|---|---|---|
| **0.4.0** | `RequestTrackingMiddleware` now `await`s `enqueue_request` instead of `asyncio.create_task`. Adds 1–2 ms per request on SQLite. | Set `track_requests=False` if the latency matters, or wait for batched inserts. |
| **0.4.1** *(planned)* | `endpoint` field of captured logs now uses the matched FastAPI route template (`/items/{id}`) instead of the literal URL (`/items/123`). Issues collapse correctly. | None — fingerprints will re-key, which means existing issues "split" once between the literal and template forms. Old rows still searchable; one-time noise in the Issues tab. |
| **0.3.0** | Issue grouping introduced. `issue_fingerprint` column added to `flare_logs`, new table `flare_issues`. | Idempotent migration runs on next start. Pre-existing rows have `issue_fingerprint = NULL` and don't appear on the Issues tab. |
| **0.2.0** | Storage backend rewritten — Redis dropped, PostgreSQL + SQLite added. | Anyone on Redis must migrate. |

## How to depend on `fastapi-flare`

### For a critical production service

```toml
[tool.poetry.dependencies]
fastapi-flare = "0.4.0"   # exact pin
```

Treat upgrades like any other dependency upgrade — read the CHANGELOG, run
your test suite, then bump.

### For a tool / experiment / dev project

```toml
[tool.poetry.dependencies]
fastapi-flare = "^0.4.0"   # accept patches, manual minor bumps
```

Caret with `^0.4.0` allows `0.4.x` (patch updates only — Poetry treats `0.x.y`
caret as bug-fix-only, which matches our patch convention).

### To track the latest

```bash
poetry add fastapi-flare@latest
```

For when you actively want the new features and accept some churn.

## Fork friendliness

If you ever need to fork:
- License is **MIT** — no friction.
- Code base is small (~30 Python files, no native code, no async-coloured
  abstractions outside FastAPI/Starlette).
- Tests are reasonably complete (62/62 covering body capture, fingerprinting,
  issue storage, and response capture).
- No vendor SDKs / proprietary dependencies. PostgreSQL via `asyncpg`, SQLite
  via `aiosqlite`, both first-party Python libs.

We acknowledge this is a single-author project; forking has been kept
intentionally cheap.
