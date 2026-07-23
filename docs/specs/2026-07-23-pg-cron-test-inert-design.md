# Spec: test-inert pg_cron

## Problem

`django_absurd.pg_cron` breaks ordinary Django testing for any downstream project that
installs it. pg_cron's `cron.database_name` is a **single server-level GUC**:
`CREATE EXTENSION pg_cron` is only legal in that one blessed DB, and its one launcher
serves only that DB. `pg_cron/migrations/0001` runs
`CREATE EXTENSION IF NOT EXISTS pg_cron` unconditionally. Consequences:

- **xdist impossible** — pytest-django gives each worker `test_<db>_gwN`; only one DB
  per server can be blessed → workers beyond it fail `CREATE EXTENSION`.
- **no clean isolation** — the test DB must equal `cron.database_name`, forcing tests
  onto the real dev DB (pollution; auto-cleanup wipes real schedules) or a second
  server.
- **mirror hazard** — a live dev launcher can fire real jobs (cleanup_all_queues, a
  scheduled enqueue) into test data if a test ever runs on the blessed DB.

Migration escape hatch ALONE is insufficient: five files touch `cron.*` (models,
reconcile, validators, flush, migration). With the extension absent, any
`ScheduledTask.save()`/ `.delete()`/`full_clean` raises
`ProgrammingError: schema "cron" does not exist`. Tests survive TODAY only because
`flush_absurd_state` accidentally swallows ProgrammingError.

## Goal

`django_absurd.pg_cron` is **test-inert by default**: a pg_cron project runs its
ordinary pytest-django suite (xdist, `test_<db>` isolation, no main-DB contact) exactly
like a non-pg_cron project — no extension, no `cron.*`. An explicit **opt-in**
re-enables the real machinery for the rare integration test (accepting single-blessed-DB
/ single-worker).

Success = the pg_cron example test (and any downstream) runs on **plain Postgres** with
no `cron.database_name` juggling. The DB-name problem disappears, not worked around.

## Non-goals

- Making pg_cron scheduling itself work under xdist (structurally impossible — one
  blessed DB per server). Opt-in mode stays single-worker.
- Client-side cron-grammar validation (stays DB-authoritative; skipped when inert).

## Design

### Detection predicate (state-based, NOT probe-based)

Reuse the existing test-DB detection behind `SYNC_SCHEDULES_ON_TEST_DB`
(`should_sync_schedules`, apps.py: `live settings_dict["NAME"]` !=
`ORIGINAL_DATABASE_NAMES[alias]` snapshot captured in `PgCronConfig.ready()`). Extract
`ORIGINAL_DATABASE_NAMES` + an `is_test_database(alias)` into a **leaf module**
(importable by migrations + models + reconcile + flush without import cycles; apps.py
imports backends/signals so it can't be the home). `ready()` still populates the
snapshot.

Liveness: `pg_cron_inert(alias)` = `is_test_database(alias) AND not PG_CRON_ON_TEST_DB`
(new backend option, default False, sibling of `SYNC_SCHEDULES_ON_TEST_DB`). Opt-in read
comes from the resolved Absurd backend's OPTIONS.

**Must be state-based** (name-snapshot), never probe-based ("does cron.job exist?"): if
a project points `TEST["NAME"]` at the blessed dev DB, the extension EXISTS there — a
probe would go live and auto-cleanup (`teardown_crons`, or the blanket
`drop_pg_cron_state` unschedule) would WIPE the dev's real schedules. State-based
inertness protects that regardless of extension presence — this also FIXES today's
latent wipe-real-jobs bug.

Works in migrations (test-DB setup migrates after `setup()` snapshot + NAME swap →
detected test) and xdist (each worker re-runs `setup()`, gets its own `test_<db>_gwN` →
detected test).

### Migration escape hatch (DECISION: edit 0001 in place)

New `django_absurd/pg_cron/operations.py`: a `CreateExtension` subclass whose
forwards/backwards no-op when `pg_cron_inert(schema_editor.connection.alias)`.
Precedent: Django's own `CreateExtension.database_forwards` already conditionally skips
(non-postgres, already-present). Swap `CreateExtension("pg_cron")` in `0001` for it.

Determinism safe: `CreateExtension` has a no-op `state_forwards` → migration graph,
autodetector, and `django_migrations` rows are byte-identical either way; only DDL
execution differs, exactly along the test/real axis where DB contents already differ.
Real deploys never re-run it. Editing shipped `0001` in place is fine here (no identity
change; pre-1.0; a migration consolidation is already planned). The `RunSQL`
wrapper-function operation stays UNCONDITIONAL — no pg_cron dependency, so the
`ScheduledTask` table + fire-wrapper exist in test DBs and ORM/admin/row assertions
work.

### Runtime gating (at the lowest cron.* sites, not signal receivers)

Gate each `cron.*`-touching function on `pg_cron_inert(...)` (there are five
signal/reconcile/flush paths; gating the receivers misses four). When inert:

- `models.ScheduledTask.schedule_pg_cron_job` / `unschedule_pg_cron_job` → early return
  (rows still save/delete for non-pg_cron assertions).
- `models.PgCronManager.get_job` → None; `get_managed_jobs` → []; `unschedule_matching`
  / `prune_jobs_without_rows` → no-op.
- `reconcile.reconcile_cleanup_job` / `unschedule_cleanup_job` → no-op.
- `validators.validate_pg_cron_cron` → skip the DB probe (grammar unvalidated when inert
  — documented).
- `flush.drop_pg_cron_state` → skip the two `cron.*` statements, KEEP the
  `ScheduledTask` truncate.

This REPLACES the accidental swallowed-ProgrammingError reliance. Auto-cleanup
(`flush_absurd_after_teardown` → `teardown_crons`) keeps running; its cron ops become
real no-ops while its row deletes still clear `ScheduledTask` between transactional
tests.

### Opt-in (settings-level) + explicit command

`OPTIONS["PG_CRON_ON_TEST_DB"]` (default False). Must be settings-level, not a
fixture/marker: the extension is created (or skipped) once at test-DB setup — a per-test
fixture can't retroactively re-migrate. A run that genuinely exercises pg_cron needs a
dedicated settings module anyway (blessed `TEST["NAME"]` = `cron.database_name`,
dedicated server, single worker) — the shape of the internal `tests/pg_cron` suite,
which sets the key and becomes the documented pattern.

`absurd_sync_crons` must not silently half-work when inert (WHY.md principle: an
explicit command should sync regardless) — it raises `CommandError` with an actionable
message ("pg_cron is inert on a test database; set OPTIONS['PG_CRON_ON_TEST_DB']=True
…").

The #101 `absurd_load_schedules` fixture sits ON TOP: requires live mode, fails loud
when inert; does not itself flip inertness.

### System checks

1. **Composition check (E-level):** `SYNC_SCHEDULES_ON_TEST_DB=True` without
   `PG_CRON_ON_TEST_DB=True` is contradictory (migrate-time sync would upsert rows whose
   job emission silently no-ops) → reject.
2. **Real-DB fail-safe check (DECISION: include).** The hardest risk is detection
   misfire on a real deploy — anything that mutates `DATABASES[...]["NAME"]` after
   `ready()` (dynamic/multi-tenant) → real DB misclassified test → `CREATE EXTENSION`
   silently skipped + fail-fast lost. Add a check that the extension IS present when the
   app is installed and the DB is classified non-test → restores fail-loud at
   `check`/deploy time.

## Mirror hazard (real crons must not fire into tests)

Structurally prevented by default: the launcher connects only to `cron.database_name`
(the blessed dev DB); default-inert tests run on `test_<db>[_gwN]`, never the blessed DB
→ the launcher can't reach test data even on a shared dev server with live schedules.
Bites only if tests are pointed AT the blessed DB — our side stays inert (state-based),
but the launcher is outside our control → surface via a W-level check reading
`current_setting('cron.database_name')` when it equals the live test DB without opt-in
(dovetails the deferred "absurd DB == cron.database_name" check). Opt-in mode IS this
hazard by construction (accepted; dedicated test server, single worker, scoped
auto-cleanup).

## Composition with existing keys

`SYNC_SCHEDULES_ON_TEST_DB` (migrate-time sync gate) and `PG_CRON_ON_TEST_DB` (machinery
liveness) are independent axes. Internal `tests/pg_cron`: `PG_CRON_ON_TEST_DB=True` +
`SYNC_SCHEDULES_ON_TEST_DB=False` (extension + signals live, migrate-sync off, tests
drive sync explicitly) — proves independence.

## Test proof (what the suite must show)

- **Inert default on plain Postgres** (the downstream scenario): a settings module with
  `pg_cron` installed, run against the plain `db` service (no extension possible).
  `migrate` succeeds (no CREATE EXTENSION),
  `ScheduledTask.save()`/`.delete()`/`full_clean` don't raise, auto-cleanup no-ops
  cleanly, `cron.job`/`cron.*` never touched. Simulates xdist (each worker = a test DB
  with no extension).
- **Opt-in live** (internal suite): `PG_CRON_ON_TEST_DB=True` on the blessed test DB —
  extension created, signals schedule real jobs, `absurd_load_schedules` works.
- **Command loud when inert:** `absurd_sync_crons` → CommandError with the full message.
- **Composition check:** `SYNC_SCHEDULES_ON_TEST_DB=True` alone → the E-level check
  fires (full msg/hint).
- **Real-DB fail-safe:** app installed, non-test DB, extension absent → the check fires.

## Change list (file-level)

- `django_absurd/pg_cron/<leaf>.py` (new) — `ORIGINAL_DATABASE_NAMES`,
  `is_test_database`, `pg_cron_inert`.
- `django_absurd/pg_cron/apps.py` — populate snapshot into the leaf module;
  `should_sync_schedules` uses it.
- `django_absurd/pg_cron/operations.py` (new) — conditional `CreateExtension`.
- `django_absurd/pg_cron/migrations/0001_initial.py` — swap the operation.
- `django_absurd/pg_cron/models.py` — gate 6 methods.
- `django_absurd/pg_cron/validators.py` — skip probe when inert.
- `django_absurd/pg_cron/reconcile.py` — gate cleanup-job (un)schedule.
- `django_absurd/flush.py` — gate `drop_pg_cron_state` cron.* (keep truncate).
- `django_absurd/pg_cron/management/commands/absurd_sync_crons.py` — CommandError when
  inert.
- `django_absurd/backends.py` — `PG_CRON_ON_TEST_DB` in `AbsurdBackendOptions`.
- `django_absurd/pg_cron/checks.py` — composition check + real-DB fail-safe + optional
  W-level blessed-DB warning.
- `django_absurd/pytest_plugin.py` — `absurd_load_schedules` fixture (#101),
  live-mode-required.
- `tests/pg_cron/settings.py` — set `PG_CRON_ON_TEST_DB=True`.
- new inert-mode tests on plain `db`; docs (AGENTS testing, WHY, README).
- Follow-up: simplify the pg_cron example test (drop `CRON_DATABASE_NAME`; run on plain
  db).

## Top risks

1. (hardest) detection misfire on real deploy → mitigated by the real-DB fail-safe check
   (above).
2. `--keepdb` + flipping opt-in on → migrations don't re-run → raw ProgrammingError; doc
   "flip needs --create-db".
3. silent behavioral divergence (inert skips grammar validation + emission) —
   documented; opt-in suite + #101 for job assertions.
4. tests aimed at blessed DB — only the W-level warning can surface (launcher not ours).
