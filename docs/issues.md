# Issue Grouping

> Introduced in **v0.3.0** (2026-04-23). No configuration required — upgrade
> and restart.

A reference guide to the Issues feature: what it does, how the fingerprint is
computed, the full API surface, migration behaviour, and the operational
workflow for resolving bugs.

## What problem it solves

The Errors tab (`/flare`) is a raw stream. A single bug that fires five hundred
times produces five hundred rows — the stream drowns in duplicates and you
can't answer "how many *distinct* problems do I have right now?" without doing
the grouping in your head.

The Issues tab (`/flare/issues`) collapses occurrences of the same
semantically-equivalent error into a single **issue** with:

- `occurrence_count` — how many times it has fired
- `first_seen` / `last_seen` — lifetime of the problem
- `level` — highest severity observed (upgrades only, never downgrades)
- `resolved` / `resolved_at` — bug workflow with automatic reopening

## How the fingerprint is computed

`fastapi_flare.fingerprint.compute_fingerprint()` returns a 16-char `blake2b`
hex string. The input depends on what's available on the captured entry:

| Scenario | Detection | Hash input |
|---|---|---|
| Has stack trace | `stack_trace is not None` (generic exception, `logger.exception`, `capture_exception`) | `"trace" \| exception_type \| endpoint \| frame1 \| ... \| frame5` |
| HTTP exception | `event.startswith("http")` and `http_status is not None` | `"http" \| status \| endpoint` |
| Validation error | `event == "validation_error"` | `"validation" \| endpoint` |
| Fallback | everything else | `event \| endpoint \| message[:200]` |

### Frame normalisation

Each stack frame is reduced to `(basename(file_path), function_name)`:

- **Absolute paths are dropped.** `/srv/app/services/user.py` and
  `/home/dev/proj/src/services/user.py` both collapse to `user.py` — changing
  your deploy path doesn't re-fingerprint.
- **Line numbers are dropped.** Moving code up or down inside the same
  function keeps the fingerprint stable.
- **Only the last 5 frames are used.** Deep call stacks don't leak internal
  framework noise into the identity of the bug.

Change the filename, the function name, or raise from a new location, and the
issue does split — that's almost always the right signal.

### Why HTTPException fingerprints don't include a stack trace

Raised `HTTPException` has no meaningful Python traceback — the throw site is
usually FastAPI's own machinery. Grouping by `(status, endpoint)` reflects how
you'd actually triage: "all the 404s on `/items/{iid}` are one thing to fix;
the 403s on `/admin` are another."

### Different fingerprint, same alert?

The alert-cooldown fingerprint inside `fastapi_flare.alerting` is deliberately
coarser (`event + endpoint`). Alerting is about throttling pings; issue
grouping is about identifying bugs. They don't share a hash.

## Data model

Three storage artefacts back the feature.

### 1. `flare_issues` (new)

```sql
-- PostgreSQL
CREATE TABLE IF NOT EXISTS flare_issues (
    fingerprint       TEXT        PRIMARY KEY,
    exception_type    TEXT,
    endpoint          TEXT,
    sample_message    TEXT        NOT NULL DEFAULT '',
    sample_request_id TEXT,
    occurrence_count  BIGINT      NOT NULL DEFAULT 1,
    first_seen        TIMESTAMPTZ NOT NULL,
    last_seen         TIMESTAMPTZ NOT NULL,
    level             TEXT        NOT NULL,
    resolved          BOOLEAN     NOT NULL DEFAULT FALSE,
    resolved_at       TIMESTAMPTZ
);
```

SQLite uses `INTEGER` for the boolean and `TEXT` for datetimes, matching the
rest of the SQLite schema.

Indexes: `(last_seen DESC)` and `(resolved, last_seen DESC)`.

### 2. `flare_logs.issue_fingerprint` (new column)

Every row in `flare_logs` gets an `issue_fingerprint TEXT` column linking it
to a `flare_issues` row. It's a logical reference — there is no foreign key
constraint so the issues table survives retention cleanup of the raw logs.

Composite index `(issue_fingerprint, timestamp DESC)` keeps the drill-down
query fast.

### 3. Upsert semantics

`push_log()` calls `storage.upsert_issue()` after every `enqueue()`:

```sql
-- PostgreSQL — simplified
INSERT INTO flare_issues (fingerprint, ..., occurrence_count, first_seen, last_seen, level, resolved)
VALUES ($1, ..., 1, $ts, $ts, $level, FALSE)
ON CONFLICT (fingerprint) DO UPDATE SET
    occurrence_count = flare_issues.occurrence_count + 1,
    last_seen = GREATEST(flare_issues.last_seen, EXCLUDED.last_seen),
    level = CASE WHEN EXCLUDED.level = 'ERROR' THEN 'ERROR' ELSE flare_issues.level END,
    resolved = FALSE,
    resolved_at = NULL;
```

Three guarantees baked in:

1. **`first_seen` is never moved.** The `ON CONFLICT` branch doesn't touch it.
2. **Level only upgrades.** Once ERROR, always ERROR.
3. **Any new occurrence reopens a resolved issue.** No silent bug suppression.

## Migration

Upgrading from 0.2.x to 0.3.0+ requires **no code changes**. On next start:

- PostgreSQL — `_build_ddl()` includes `ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS issue_fingerprint TEXT` and `CREATE TABLE IF NOT EXISTS flare_issues
  ...`. Safe to re-run; safe across multiple workers starting concurrently.
- SQLite — `_ensure_db()` runs the new DDL and tries
  `ALTER TABLE logs ADD COLUMN issue_fingerprint TEXT` in a try/except (SQLite
  has no `IF NOT EXISTS` for ADD COLUMN; subsequent attempts raise and are
  swallowed).

Existing rows in `flare_logs` keep `NULL` in the new column — no backfill.
They don't appear on the Issues tab, which is intentional: the tab reflects
live grouping from the point of upgrade forward.

## Dashboard walkthrough

Open `/flare/issues`:

1. **Four stat cards** — Open, New (24h), Resolved (7d), Total.
2. **Status filter chips** — All / Open / Resolved.
3. **Search box** — matches `exception_type`, `endpoint`, or `sample_message`
   with case-insensitive substring search.
4. **Table columns** — Level, Exception, Message (sample), Endpoint, Count,
   Last seen, Status, chevron.
5. **Click a row** — modal opens showing the issue metadata (first/last seen,
   count, fingerprint) and a paginated list of occurrences.
6. **Click an occurrence** — swaps to the same detail view used by the Errors
   tab: full stack trace, request body, headers, context. A "Back to issue"
   button returns you to the occurrence list.
7. **Resolve / Reopen** — button in the modal header area. The change is
   reflected immediately in stats and in the filtered list.

## JSON API reference

All endpoints live under `{dashboard_path}/api/issues` (default
`/flare/api/issues`) and share the same auth as the rest of the dashboard API
(`dashboard_auth_dependency`, Zitadel bearer/browser, or no auth).

### `GET /flare/api/issues`

List issues. All query params are optional.

| Param | Type | Description |
|---|---|---|
| `page` | int, ≥1 | 1-indexed page. Default `1`. |
| `limit` | int, 1–500 | Items per page. Default `50`. |
| `resolved` | bool | Filter by status. Omit for all. |
| `search` | string | Substring match on `exception_type`, `endpoint`, or `sample_message`. |

Response — `FlareIssuePage`:

```json
{
  "issues": [
    {
      "fingerprint": "63d05fb9d296a8e1",
      "exception_type": "ValueError",
      "endpoint": "/boom/value-error",
      "sample_message": "must be positive, got -1",
      "sample_request_id": "ab12...",
      "occurrence_count": 42,
      "first_seen": "2026-04-23T10:00:00Z",
      "last_seen":  "2026-04-23T11:27:18Z",
      "level": "ERROR",
      "resolved": false,
      "resolved_at": null
    }
  ],
  "total": 12,
  "page": 1,
  "limit": 50,
  "pages": 1
}
```

### `GET /flare/api/issues/stats`

Counts used by the stat cards.

```json
{
  "total": 12,
  "open": 10,
  "resolved": 2,
  "new_last_24h": 3,
  "resolved_last_7d": 2
}
```

### `GET /flare/api/issues/{fingerprint}`

Issue detail + paginated occurrences. Query params `page` and `limit`
control the occurrences pagination (same defaults as `/flare/api/logs`).

Response — `FlareIssueDetail`:

```json
{
  "issue": { /* FlareIssue */ },
  "occurrences": {
    "logs": [ { /* FlareLogEntry with issue_fingerprint set */ } ],
    "total": 42,
    "page": 1,
    "limit": 50,
    "pages": 1
  }
}
```

Returns `404` if the fingerprint is unknown.

### `PATCH /flare/api/issues/{fingerprint}`

Toggle the `resolved` flag.

Body:

```json
{ "resolved": true }
```

Response — `FlareStorageActionResult`:

```json
{ "ok": true, "action": "issue_status", "detail": "Issue resolved" }
```

Resolving sets `resolved_at = NOW()`. Reopening clears it.

## The demo app

`examples/example_issues.py` is a self-contained app that covers every
scenario. Run it:

```bash
poetry run uvicorn examples.example_issues:app --reload --port 8003
```

Traffic recipes:

```bash
# Same ValueError 10×  →  1 issue, count=10
for i in {1..10}; do curl -s http://localhost:8003/boom/value-error >/dev/null; done

# Different type, different issue
for i in {1..5};  do curl -s http://localhost:8003/boom/key-error   >/dev/null; done

# Same type (ValueError) but different call site → separate issue
for i in {1..3};  do curl -s http://localhost:8003/boom/deep        >/dev/null; done

# 404 on a real endpoint (FastAPI typed the path — it's /items/{iid})
curl -s http://localhost:8003/items/999 >/dev/null

# 422 from Pydantic
curl -s -X POST http://localhost:8003/signup \
  -H 'Content-Type: application/json' -d '{"email":"x","password":"y"}' >/dev/null

# 500 from POST body rule
curl -s -X POST http://localhost:8003/orders \
  -H 'Content-Type: application/json' \
  -d '{"item_id":1,"quantity":1,"unit_price_cents":1}' >/dev/null

# Non-HTTP capture
curl -s http://localhost:8003/trigger/manual >/dev/null

# Volume test — generate 50 errors of mixed types
curl -s http://localhost:8003/stress/50 >/dev/null
```

Then:

```bash
# Inspect via API
curl -s http://localhost:8003/flare/api/issues | jq '.total, .issues[0]'

# Resolve an issue by fingerprint
FP=$(curl -s http://localhost:8003/flare/api/issues | jq -r '.issues[0].fingerprint')
curl -s -X PATCH http://localhost:8003/flare/api/issues/$FP \
  -H 'Content-Type: application/json' -d '{"resolved": true}'

# Fire the same endpoint again → watch it reopen
curl -s http://localhost:8003/boom/value-error >/dev/null
curl -s http://localhost:8003/flare/api/issues/$FP | jq '.issue.resolved'
# false  — reopened
```

## Operational notes

- **Dashboard auth** — the Issues routes use the same `dashboard_auth_dependency`
  (or Zitadel flow) as the rest of the dashboard. No new configuration.
- **Multi-process / multi-worker** — the `flare_issues` UPSERT is atomic per
  backend, so concurrent workers writing the same fingerprint produce the
  correct `occurrence_count` without a race.
- **Retention interplay** — `retention_hours` purges raw `flare_logs` rows.
  The corresponding `flare_issues` rows keep counters; the drill-down list in
  the modal just shrinks. Future versions may add separate retention for
  issues themselves.
- **Storage overhead** — one extra row per distinct bug plus one extra TEXT
  column on each log row. For 10k logs across 50 issues, that's well under
  1 MB in both backends.

## Known limitations

- **Dynamic path params** — `/items/1` and `/items/2` currently produce
  separate issues because handlers capture `request.url.path` (the literal
  URL). The matched FastAPI route template (`/items/{iid}`) is the correct
  grouping key; this is a planned follow-up and will collapse those into one
  issue without breaking the API shape.
- **SQLite multi-tenancy** — `flare_issues` uses a fixed table name on
  SQLite (as do `logs`, `requests`, `flare_settings`, `flare_metrics_snapshots`).
  If you need per-project tables on SQLite, run one `.db` file per project.
  PostgreSQL derives `flare_issues_<suffix>` from `FLARE_PG_TABLE_NAME` like
  everything else.
- **No backfill** — upgrading a running deployment leaves existing rows with
  `issue_fingerprint = NULL`; the tab starts populating on the next captured
  error.

## Related modules

- `fastapi_flare/fingerprint.py` — the hash itself.
- `fastapi_flare/queue.py` — `push_log()` wires fingerprint + upsert into the
  normal write path.
- `fastapi_flare/storage/base.py` — Protocol additions:
  `upsert_issue`, `list_issues`, `get_issue`, `list_logs_for_issue`,
  `update_issue_status`, `get_issue_stats`.
- `fastapi_flare/storage/pg_storage.py` / `sqlite_storage.py` — concrete
  implementations.
- `fastapi_flare/templates/issues.html` — the dashboard UI.
- `tests/test_fingerprint.py` / `tests/test_issues_storage.py` — 30 tests
  covering determinism, refactor robustness, and CRUD on both backends.
