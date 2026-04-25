# Changelog

All notable changes to **fastapi-flare** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — 2026-04-24

### Added — Response body capture
Opt-in snapshot of what the endpoint responded with, so observability covers
both sides of a request. Default stays off to avoid privacy/volume surprises.

- `capture_response_body: bool = False` — master switch
- `capture_response_body_min_status: int = 400` — errors-only by default
- `max_response_body_bytes: int = 8192` — truncation cap
- `response_body_retention_hours: int = 24` — auto null-out TTL (row stays for
  metrics, only the payload is cleared)
- New `response_body` column on `flare_logs` and `flare_requests`. Idempotent
  migrations on both PG (`ADD COLUMN IF NOT EXISTS`) and SQLite (try/except
  `ALTER TABLE`). Existing deployments get the column on next start.
- `FlareLogEntry` and `FlareRequestEntry` schemas gained `response_body`.
- Capture respects `sensitive_fields` — values at matching keys are redacted
  before storage.
- Binary / streaming content-types skipped (`image/*`, `video/*`, `audio/*`,
  `application/octet-stream`, `application/pdf`, `application/zip`,
  `application/x-*`, `text/event-stream`, `multipart/*`).
- Streaming response bytes still reach the client intact (buffering was
  already inherent to `BaseHTTPMiddleware`; rebuild preserves the bytes).
- Dashboard modals (Errors / Issues / Requests) gained a "Response Body"
  section mirroring Request Body.
- 10 new tests under `tests/test_response_body.py`.

### Changed — RequestTrackingMiddleware awaits `enqueue_request` inline
Dropped the `asyncio.create_task` fire-and-forget in favour of `await`.
Adds 1–2 ms per request on SQLite, negligible on PostgreSQL, and eliminates
a race that lost rows under `TestClient`'s sub-loop model. `enqueue_request`
continues to swallow its own errors so the request path stays non-raising.

### Changed — UI / Design pass
Significant polish across the dashboard templates. Markup unchanged, only
`_styles.html` and `layout.html`.

- **Layout refactor**: sidebar collapsed to a 56px icon-only rail that
  expands to 220px on hover (overlay — no main-content reflow). Drill-down
  modal docks as a right-edge drawer (slide-in) instead of a centred card;
  the table behind stays visible so entry-to-entry browsing feels natural.
- **Palette**: final pass on neutral darks (`#09090b` / `#0f0f11` /
  `#131316`). Semantic red (`#ef4444`) reserved for ERROR, brand flame, and
  active indicators. Cool zinc grays replace warm beige tokens that
  DarkReader was mangling to cream.
- **DarkReader opt-out**: added `<meta name="color-scheme" content="dark">`
  and `<meta name="darkreader-lock">` to `layout.html` so the extension
  leaves our already-dark page alone.
- **Design tokens**: centralised radii (`--r-sm/md/lg/xl`), shadows
  (`--shadow-sm/md/lg/xl`), motion (`--ease`, `--ease-out`, `--t-fast/base/slow`),
  and focus rings (`--ring`).
- **Typography**: Inter stylistic alternates (cv02/03/04/11), tabular
  numerals on stats/timestamps, consistent 1.5 line-height baseline.
- **Focus rings** visible on all interactive elements via `:focus-visible`.
- **Sticky table headers** — `thead` stays pinned while scrolling long lists.
- **Cards** (stats, table, filters) use a top gradient line for depth
  instead of heavy left-stripes.
- **Animations**: subtle fade-up on main children at first paint (staggered
  40ms), smoother modal open timing.
- **Smaller header** (52px), refined pagination (ghost buttons), nicer
  empty states, `<kbd>` styling prepared for future shortcut hints.

## [0.3.4] — 2026-04-24

### Changed — tone down the Errors table
The 0.3.3 palette stacked too much orange inside the Errors table: every row
had an orange border-left, an orange badge, and hovering tinted the whole row
orange again — the combination read as a wall of orange. Cleaned up:

- **Row hover** is now neutral (`rgba(255,255,255,0.025)`) instead of orange,
  so hover does not compound with the accent stripe on `level-error` rows.
- **Level stripe** on error/warning rows dropped from solid `border-left` to
  `inset box-shadow` at 55% opacity. Reads as an accent, not a paint bar, and
  no longer competes with cell padding.
- **`.badge-level`** shrank back from pill (99px radius, 10px padding) to a
  compact rounded rectangle (6px radius, 3×8px padding, 9px font) — closer
  to the original density and the mockup's "Critical" chip.
- **Stat-card left stripe** softened to 55% opacity; removed the outer orange
  drop-glow (`-3px 0 24px`) that was bleeding into the surrounding card.
- **Stat-value.red** switched from saturated `#ff5f1f` to the peach tint
  `#ffb59c` — big numbers are sharper and less alarm-colored.

No markup change. Tests unchanged (52/52).

## [0.3.3] — 2026-04-24

### Changed — Kinetic Obsidian palette refresh (pure CSS)
Dashboard gets a visual refresh inspired by a "Kinetic Obsidian" command-center
aesthetic. No markup or behaviour changes — only `_styles.html` and
`layout.html` (fonts).

- **Primary accent** switched from red (`#dc2626`) to **Kinetic Flare orange**
  (`#ff5f1f`), better matching the product name (flare = orange flame). The
  `--red*` CSS variable names are preserved as aliases for the accent to avoid
  churn across templates.
- **Obsidian background stack** (`#030303` → `#0e0e0e` → `#141313`) replaces
  the previous flat greys.
- **Typography** now loads `Inter` and `JetBrains Mono` from Google Fonts
  (already the fallback stack, now explicit). Uppercase labels get real
  letter-spacing; mono font is used consistently on timestamps, badges,
  chips, fingerprints, endpoints.
- **Radii** bumped: cards/table/filters/modal from ~10–12px to 20–24px.
- **Glass feel** on header, sidebar, modal, toast via `backdrop-filter: blur`.
- **Atmospheric underglow** — two fixed-position blurred circles (violet +
  orange) behind the main content for depth.
- **Buttons**: primary buttons now use a subtle gradient and a soft drop-glow.
- **Live dot** got a scale-based `pulse-ring` animation and drop-shadow.
- **Stat cards** got a hover lift + violet radial underglow on hover.
- **Nav items** animate their icons left→right on hover; active state gets
  inset orange shadow.

Existing users see the new look on their next refresh. All tests pass
unchanged (52/52).

## [0.3.2] — 2026-04-24

### Added
- Documentation: new reference guide [`docs/issues.md`](docs/issues.md)
  covering fingerprint internals, storage model, migration behaviour, full
  JSON API reference, and operational notes.
- `CHANGELOG.md` added to the repo root.

### Changed
- `README.md` — expanded the Issue Grouping section and linked to the full
  reference. `FlareLogEntry` schema now shows the `issue_fingerprint` field.

## [0.3.1] — 2026-04-23

### Added
- `examples/example_issues.py` — demo app exercising every grouping scenario
  (same exception N×, same type different stack, 4xx/5xx, validation, manual
  `capture_exception`, plus a `/stress/{n}` noise generator).

### Changed
- `exception_type` shown on the Issues tab is now human-readable:
  `RequestValidationError` for 422s and `HTTP 404` / `HTTP 403` / ... for
  raised `HTTPException`, instead of the first token of the `error` field
  (which for validation errors was a pydantic field path like `body -> email`).

## [0.3.0] — 2026-04-23

### Added — Issue grouping
- New `Issues` tab at `/flare/issues` — grouped view, Sentry-style.
- New module `fastapi_flare.fingerprint` with a deterministic
  `compute_fingerprint()` based on
  `(exception_type, endpoint, top-5 normalised stack frames)`. Line numbers
  and absolute paths are stripped so ordinary refactors do not re-fingerprint
  the same bug.
- New table `flare_issues` (auto-migrated on both SQLite and PostgreSQL)
  with `fingerprint` PK, `occurrence_count`, `first_seen`, `last_seen`,
  `level`, `resolved`, `resolved_at`.
- New column `issue_fingerprint` on `flare_logs` (idempotent `ADD COLUMN IF
  NOT EXISTS` for PG, conditional `ALTER TABLE` for SQLite).
- New JSON endpoints:
  - `GET  /flare/api/issues` — paginated list with `resolved` / `search` filters.
  - `GET  /flare/api/issues/stats` — counts for the stat cards.
  - `GET  /flare/api/issues/{fingerprint}` — detail + paginated occurrences.
  - `PATCH /flare/api/issues/{fingerprint}` — toggle `resolved`.
- Resolved issues **reopen automatically** when the same fingerprint fires again.
- Level **upgrades only** — a WARNING issue that later hits ERROR becomes ERROR
  for good; subsequent WARNINGs don't downgrade it.
- Issue state **survives retention**: `occurrence_count`, `first_seen`,
  `last_seen` persist even after raw logs are purged by `retention_hours`.
- New Pydantic models: `FlareIssue`, `FlareIssuePage`, `FlareIssueDetail`,
  `FlareIssueStats`, `FlareIssueStatusRequest`.
- `FlareLogEntry` gained `issue_fingerprint: str | None`.
- Storage Protocol (`FlareStorageProtocol`) gained: `upsert_issue`,
  `list_issues`, `get_issue`, `list_logs_for_issue`, `update_issue_status`,
  `get_issue_stats`.

### Tests
- 30 new tests (`tests/test_fingerprint.py` + `tests/test_issues_storage.py`)
  covering determinism, robustness to line-number shifts, and CRUD on both
  backends.

### Known limitations
- Dynamic path params (`/items/1` vs `/items/2`) currently produce separate
  issues because handlers capture `request.url.path` literally. A follow-up
  will resolve the matched route template.
- Logs captured before 0.3.0 carry `issue_fingerprint = NULL` and do not
  surface on the Issues tab. Traffic after upgrade populates the grouping
  from that point.

## [0.2.2] — 2026-04-23

### Added
- `flare_metrics_snapshots` table — opt-in persistence of the in-memory
  metrics aggregator so multi-worker / multi-pod deployments see each other's
  aggregates and the dashboard survives process restarts.
- Dashboard now merges persisted metrics snapshots at render time.
- Latency tracking switched from a bounded deque to a **mergeable histogram**,
  enabling cross-worker P95 without sample bias.
- Non-HTTP error capture: new `capture_logging`, `capture_asyncio_errors`,
  `capture_exception()` and `capture_message()` public API. Errors from
  background tasks, workers, cron jobs, and detached asyncio tasks now land
  on the dashboard.
- Notification settings system with built-in Slack / Discord / Teams /
  generic webhook notifiers, with per-`(event, endpoint)` cooldown.
- Documentation for request tracking options (`track_2xx_requests`,
  `FLARE_TRACK_2XX_REQUESTS`).

## [0.2.0] — 2026-04

### Added
- HTTP Requests tab with a ring-buffer store (`flare_requests`) and
  linkage to error logs via `request_id`.
- SQLAlchemy async ORM example (SQLite + PostgreSQL).
- Componentised Jinja2 templates (`_macros.html`, `_styles.html`,
  `_scripts_global.html`, `layout.html`).

### Changed
- Retention cleanup now runs on a throttled schedule
  (`retention_check_interval_minutes`, default 60) rather than on every
  worker tick.

### Performance
- Missing composite indexes added to both backends.

## [0.1.5] — previously

### Changed
- Storage backend rewritten: **Redis replaced by PostgreSQL** for production,
  **SQLite** promoted to the zero-config default.
- Removed live-feed page.

### Added
- Uptime tracking on `FlareWorker`.
- Enhanced Zitadel token exchange.

## [0.1.4 and earlier]

See `git log` for the historical series covering initial release, Zitadel
OAuth2 setup (bearer + browser PKCE), dashboard layout, metrics tab, and
the request-body capture fix (`BodyCacheMiddleware`).

[0.4.0]: https://github.com/londarks/fastapi-flare/compare/v0.3.4...v0.4.0
[0.3.4]: https://github.com/londarks/fastapi-flare/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/londarks/fastapi-flare/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/londarks/fastapi-flare/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/londarks/fastapi-flare/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/londarks/fastapi-flare/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/londarks/fastapi-flare/compare/v0.1.5...v0.2.2
[0.2.0]: https://github.com/londarks/fastapi-flare/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/londarks/fastapi-flare/compare/v0.1.4...v0.1.5
