# Spec: cross-database pg_cron scheduling (test-safe by construction)

> Supersedes the earlier "test-inert pg_cron" approach (kept in git history). Same
> problem, better solution: instead of installing the extension in the app DB and gating
> it in tests, the app DB **never** holds the extension — pg_cron schedules across DBs
> from one central metadata DB. Decision 2026-07-24; verified live against pg_cron 1.6.

## Problem

pg_cron's `cron.database_name` is a **single server-level GUC**;
`CREATE EXTENSION pg_cron` is only legal in that one designated database
(cron.database_name). Today `django_absurd.pg_cron` assumes `cron.database_name` == the
app/absurd DB — the extension, `cron.job` metadata, and the
`django_absurd_run_scheduled` fire-wrapper all live there, and `pg_cron/0001` runs
`CREATE EXTENSION` on the app DB. So every app/test DB must be the pg_cron database,
which:

- **breaks pytest-xdist** — each worker gets `test_<db>_gwN`; only one DB per server can
  be pg_cron → other workers fail `CREATE EXTENSION`;
- **breaks standard `test_` isolation** — the test DB must equal `cron.database_name`,
  forcing tests onto the real dev DB (pollution; auto-cleanup can wipe real schedules)
  or a second server;
- **invites a mirror hazard** — a live launcher firing real jobs into test data.

## Decision & goal

Adopt **`cron.schedule_in_database`**. `cron.database_name` becomes a **central metadata
DB (≠ the app DB)** holding the extension + `cron.job`; jobs are scheduled from there to
**run in the current app DB** (`database => <app db>`). The app/absurd DB — and
therefore every `test_<db>` — **never holds the extension and never touches `cron.*`**.
Tests then run ordinary pytest-django (xdist, `test_` isolation, no main-DB contact)
with no special handling of the app DB. Live scheduling also becomes xdist-safe and
multi-project-safe.

## Verified pg_cron facts this relies on (live, pg_cron 1.6; floor 1.4)

See the `pg-cron` skill. Load-bearing:

- `cron.schedule_in_database(name, sched, cmd, database, username DEFAULT NULL, active DEFAULT true)`
  — full signature (incl. `active`) since **1.4** (already our floor). Gate on
  `to_regproc('cron.schedule_in_database')`, not version parsing.
- **The target DB needs NO extension** — a job scheduled into an extension-less DB both
  schedules AND fires (launcher connects to the target, runs the command there,
  `job_run_details.status='succeeded'`).
- **Jobs are bound to their target DB** (`cron.job.database`): a `database => main_db`
  job fires only into `main_db`, never a test DB. → the mirror hazard is structurally
  gone, and teardown filters by `WHERE database = '<test_db>'`.
- **Upsert-steal (PROVEN, critical):** `schedule_in_database` on an existing
  `(jobname, username)` silently retargets that jobid to the new database (`cron.job`
  UNIQUE is `(jobname, username)`, NOT scoped by database; RLS
  `username = current_user`). → jobnames MUST be namespaced by target DB.
- `current_setting('cron.database_name', true)` is readable from any DB on the server
  (NULL on a non-pg_cron server) → the central DB name is auto-discoverable,
  zero-config.
- Non-superuser needs `GRANT USAGE ON SCHEMA cron` +
  `GRANT EXECUTE ON FUNCTION cron.schedule_in_database(...)` in the central DB (shipped
  un-granted on purpose). **`cron.alter_job` is ALSO EXECUTE-revoked (owner-only,
  verified live).** So **fold the `active` flag into `schedule_in_database`'s 6th
  argument and DROP the current post-schedule `cron.alter_job(...)` call**
  (`models.py:273-275`) — then those two grants are genuinely complete. (`cron.job` /
  `cron.job_run_details` are SELECT/DELETE to PUBLIC under RLS, so the seam's reads +
  scoped `DELETE FROM cron.job_run_details WHERE database=…` need no extra grant.)
- `DROP DATABASE` on a job's target succeeds (launcher holds no session on non-metadata
  DBs) → the `--create-db` eviction hack disappears for app test DBs.
- **End-to-end scenario PROVEN live** (pg_cron example, central
  `cron.database_name=postgres`, `CreateExtension` temporarily removed): a 1-second
  `pingpong` scheduled onto the main `demo` DB kept firing into `demo` throughout a full
  run of the example pytest suite, **never** fired into `test_demo`, and **survived**
  the run + auto-cleanup — while the suite migrated `test_demo` with NO
  `CREATE EXTENSION` and passed. This is the whole thesis, confirmed.

## Architecture

### Central metadata DB + operator setup (no CreateExtension migration)

**Drop `CreateExtension("pg_cron")` from `pg_cron/0001` outright** (state-neutral op
removal — safe for already-applied deployments; the conditional-operation machinery the
old approach needed falls away). The `RunSQL` wrapper (`django_absurd_run_scheduled`)
stays in the app-DB migration — it runs in the app DB, needs no extension. The extension
becomes **one-time operator setup on `cron.database_name`**: `shared_preload_libraries`,
one `CREATE EXTENSION pg_cron`, + the two grants. django-absurd runs NOTHING in the
central DB except `cron.*` function calls (no migration, no wrapper there). **Operator
requirement: migrate (settings/admin reconcile) and the admin web app MUST use ONE
scheduling role** — `cron.job` is UNIQUE on `(jobname, username)` with RLS
`username = current_user`, so two roles scheduling the same logical job → duplicate jobs
(double-fire) + teardown blind spots. Document one scheduling role for the Absurd DB.

### Central connection model (the crux)

Do **NOT** add a `DATABASES` alias — `setup_databases` test-swaps every alias, so a
central alias would become an extension-less `test_<central>`. Instead: a **raw,
short-lived psycopg connection** built from
`connections[resolve_absurd_database()].get_connection_params()` with `dbname` swapped
to the central name. Central name = `OPTIONS["CRON_DATABASE_NAME"]` (a DB _name_) if
set, else auto-discovered via `current_setting('cron.database_name', true)` on the app
connection. Same-server is correct by construction (pg_cron is server-local). Scheduling
ops are rare (migrate reconcile, admin save, teardown) so per-op connect cost is fine.
The exact pattern already runs green twice here (`worker.py:171-175` async — pops
`cursor_factory`; `tests/utils.py:72-73` sync). **Commit discipline:** raw psycopg
defaults `autocommit=False` — every write op MUST commit and the grammar probe MUST roll
back; use `autocommit=True` + an explicit per-op `conn.transaction()`, or a forgotten
commit is a silent no-schedule bug. `get_connection_params()` carries SSL `OPTIONS`
through (sslmode inherited); a dbname-routing pooler (pgbouncer) or an
`OPTIONS["service"]` setup needs the central DB reachable — one docs line.

### One seam for all `cron.*`

Route all eleven current `cron.*` sites (`models.py` PgCronManager ×4 +
schedule/unschedule; `reconcile.py` cleanup-job (un)schedule; `validators` grammar
probe; `flush.drop_pg_cron_state`) through **one new module** (`pg_cron/catalog.py`)
exposing verbs (`schedule_job`, `unschedule_jobs_for_database`, `get_job`,
`probe_cron_grammar`, …) that open the central connection. The one remaining test-gate
lives here — one gate, not five.

**Error contract (BLOCKING — B1):** a raw psycopg connection raises `psycopg.*`, NOT
Django's `django.db.utils.*` wrappers (Django only wraps errors on its own registered
connections). `catalog.py` MUST translate psycopg exceptions into the Django hierarchy
at the seam boundary — otherwise the existing best-effort catch nets miss them and
crash: `apps.py:103-114` (migrate-never-breaks → a wrong/unreachable
`CRON_DATABASE_NAME` crashes migrate), `flush.py:37` (test teardown → every transaction
test errors in `_post_teardown`), `validators.py:100` (grammar probe → a 500 in the
admin instead of a form error).

### Jobname namespacing (mandatory, defeats upsert-steal)

`_dj:<target_db>:<source>:<name>`. Teardown double-scoped:
`WHERE database = %s AND starts_with(jobname, '_dj:' || %s || ':')` — use `starts_with`,
NOT `LIKE` (`_` is a LIKE wildcard and appears in every test DB name like `test_x_gw1`;
the existing code already uses `starts_with`). Collision-free across xdist workers,
multiple projects, and test-vs-prod on one server. The 63-byte jobname rule in
`validate_jobname_length` is relaxable (`cron.job.jobname` is untruncated `text`); keep
a sane cap; static validation uses the configured prod `NAME` (runtime `_gwN` suffixes
are not statically known).

### Emission timing + serialization (a real regression to manage)

Today `post_save` schedules on the row's own connection under `pg_advisory_xact_lock`
(`open_locked_cursor`) → row upserts AND cron writes are one atomic, serialized
transaction. Central = a second connection, so: (a) emission happens after commit (via
**`transaction.on_commit`**), outside any lock; (b) the advisory lock now serializes
only the central op, not the row write → concurrent reconcilers' row upserts can race
(survivable: `update_or_create` handles the IntegrityError) and two writers can emit
out-of-commit-order (older cadence lingers until the next reconcile). State this new
serialization story explicitly — "the lock moves onto the central connection" is NOT
equivalence.

**Why lost atomicity is acceptable (load-bearing):** the run-wrapper (`0001`
`CREATE_FN`) RE-READS the `ScheduledTask` row on every fire
(`NOT FOUND OR NOT enabled → RETURN`). So divergence can only ever be a MISSED fire or a
STALE cadence — never wrong args/queue/enabled, never an orphan spawn after delete.
No-worse-than-today in KIND, only in timing.

Heal points: the settings lane heals at every migrate / `absurd_sync_crons`; the ADMIN
lane heals only at `post_migrate` → `sync_admin_crons` (`apps.py:47`) — potentially
weeks, so admin saves should opportunistically re-run admin-lane healing. On an
on-commit central failure AFTER the row committed: **swallow-and-log** (row saved, job
missing, healed next reconcile) — do NOT propagate a 500 for an already-saved row.
Rewrite `signals.py`'s contract docs.

### Cleanup job

`reconcile_cleanup_job` moves central too:
`schedule_in_database(<db-namespaced cleanup name>, cron, CLEANUP_COMMAND, database => app_db)`
(`absurd.cleanup_all_queues` runs in the app DB). The `absurd_cleanup_all` shared
identity becomes db-namespaced — deliberately breaking the shared name with
`absurd.enable_cron` / `absurdctl cron`, acceptable because under central topology those
same-DB functions can't run in the app DB anyway. Absurd-native partition/detach jobs
stay unsurfaced (status quo).

### Scoped flush (was a blanket wipe)

`flush.drop_pg_cron_state` currently blanket `cron.unschedule(jobid) FROM cron.job` +
`TRUNCATE cron.job_run_details`. Against a SHARED central catalog that is catastrophic —
rewrite scoped: unschedule `WHERE database = <ours>`; replace the blanket `TRUNCATE`
with `DELETE FROM cron.job_run_details WHERE database = <ours>`. **"ours" = the LIVE db
name** (`current_database()` / live `settings_dict["NAME"]`), NEVER
`ORIGINAL_DATABASE_NAMES` — so xdist `gw1` scopes to `test_x_gw1`, another project's /
prod's rows carry a different `database` value and are untouchable (RLS by username adds
a second scope). This closes the catastrophic path.

### Backward compatibility (free)

Same-DB is the **degenerate case**: GUC auto-discovery on an existing deployment returns
the app DB itself → the central connection lands on the app DB →
`schedule_in_database(database => current)` ≡ today's `cron.schedule`. Existing users
keep working with **zero reconfiguration, one code path, no flag**. Their
already-present app-DB extension is harmless (there the app DB _is_
`cron.database_name`).

**Transition sweep (BLOCKING — B2):** jobnames gain the db-namespace, and a NAIVE
reconcile does NOT converge — the existing prune paths are prefix-scoped to
`_dj:<source>:` / `_dj:<db>:<source>:`, so old `_dj:s:foo` never matches the new prefix
and is never pruned → BOTH jobs fire → **every schedule (and the cleanup job)
double-fires** on a same-DB user's first migrate (the run-wrapper re-reads the row, so
both spawns are live). Design an explicit one-time sweep in reconcile: unschedule
`WHERE database = <ours> AND starts_with(jobname,'_dj:') AND NOT starts_with(jobname,'_dj:'||<ours>||':')`
plus the literal old `absurd_cleanup_all` name, THEN emit the new-scheme jobs. Add it to
the change list.

## Test story — the thin residue

The app/test DB never touches `cron.*`, so most of the old gating is gone. What remains
is a small policy layer (still one gate, in `catalog.py`):

- **Detection leaf** — inert when `test_environment_active() OR is_test_database(alias)`
  (EITHER, **not AND**: `test_environment_active()` — `django.test.utils` sets
  `_TestState.saved_data` in every pytest-django / `manage.py test` run,
  per-xdist-worker, NEVER in a real deploy → prod can't misfire — alone guarantees
  prod-safety; requiring BOTH would go LIVE when a project pins `TEST["NAME"] == NAME`,
  as `tests/pg_cron/settings.py` effectively does). Guard the private `_TestState`
  access with a loud `install_absurd_cleanup`-style version-check.
- **on_commit caveat:** `transaction.on_commit` callbacks NEVER run under a
  non-transactional `db` test (no commit). So emission is exercised only under
  `transaction=True` / `django_capture_on_commit_callbacks`; the `absurd_load_schedules`
  fixture must commit or call the catalog directly (creating a row alone schedules
  nothing under `db`). Existing emission tests convert accordingly.
- **Opt-in** `OPTIONS["PG_CRON_ON_TEST_DB"]` (default False) to allow schedule creation
  from tests (writing to the shared central catalog is opt-in). No longer must be
  settings-level (no migrate-time DDL remains) — a fixture/marker opt-in is now
  possible; settings-level stays the simplest default. The #101 `absurd_load_schedules`
  fixture sits on top.
- `validate_pg_cron_cron` skips its probe when inert (avoids requiring a pg_cron server
  in every CI run).
- `absurd_sync_crons` (and `--teardown`) → `CommandError` when inert.
- **Composition check** — `SYNC_SCHEDULES_ON_TEST_DB=True` without opt-in → reject.

**Eliminated vs the old approach:** conditional `CreateExtension` + all migration
gating; five-site scattered gating (→ one seam); the mirror-hazard + W-level
pg_cron-database checks (structurally gone); the "opt-in stays single-worker" non-goal
(live opt-in is xdist-safe via db-namespaced jobs + `WHERE database` teardown); test-DB
juggling + the `--create-db` eviction procedure.

## System checks

1. **Composition (E):** `SYNC_SCHEDULES_ON_TEST_DB=True` without
   `PG_CRON_ON_TEST_DB=True`. (These are two knobs for one intent; pre-1.0 is the moment
   to consider collapsing/subordinating them rather than papering over with the check.)
2. **Central-extension fail-safe (E, `Tags.database`):** when non-test + the pg_cron
   scheduler is configured → the central DB is reachable and has the extension.
   Registered with `Tags.database` so it runs at `migrate` / `check --database` only
   (plain `check` stays DB-free). This replaces today's migrate-time `CREATE EXTENSION`
   fail-fast.

## WHY.md deliverable

**Reverse** the current "Extension in the app migration (fail-fast)" section. New
rationale to capture (via `capture-why` at ship time): we deliberately do NOT
`CREATE EXTENSION` in a migration — the single-pg_cron-database GUC makes an app-DB
`CREATE EXTENSION` break pytest-xdist / `test_` isolation; the extension is
operator-managed once on `cron.database_name`, jobs run cross-DB via
`schedule_in_database`, and fail-fast moved from migrate-time DDL to an E-level
`Tags.database` check. Also record: schedules are db-namespaced (upsert-steal);
django-absurd is the sole scheduler; the mirror hazard is gone by target-DB binding.

## Risks (ranked)

1. **Upsert-steal** (PROVEN) — mitigated fully by db-namespaced jobnames; residual ≈
   none.
2. **Lost row↔job atomicity** (two connections) — `on_commit` + reconcile self-heal;
   small self-correcting window.
3. **Crash-during-test orphans** — central rows targeting a dropped `test_*_gwN` fail
   each fire (spamming central `job_run_details`), never fire elsewhere. Recovery:
   session-start
   - teardown sweep `WHERE database = <current test db>`; optionally prune own jobs
     whose `database NOT IN (SELECT datname FROM pg_database)`.
4. **New operator surface** — central-DB `CREATE EXTENSION` + two grants + a role with
   rights in both DBs; needs docs + the E-check.
5. **Detection misfire on prod** — much softer (no DDL; worst case schedules not
   created, surfaced by the E-check); keep the `test_environment_active` conjunct
   regardless.
6. **Central `job_run_details` growth** — shared table, not ours to truncate; operator
   retention (standard pg_cron practice); risk 3's sweep bounds test contribution.

## Non-goals

- Absurd-native partition/detach cron surfacing (status quo; would need upstream
  `schedule_in_database` support).
- No absurd-sdk / absurdctl change (all scheduling is django-absurd's own SQL).

## Change list (file-level)

`pg_cron/catalog.py` (new seam) · `pg_cron/<leaf>.py` (detection: `is_test_database` +
`test_environment_active`) · `pg_cron/models.py` (fold `active` into
`schedule_in_database`, DROP `alter_job`; db-namespaced jobnames; `starts_with`
teardown), `reconcile.py` (route via catalog + the **transition sweep**, B2),
`validators.py`, `signals.py` (contract rewrite; `on_commit`; swallow-and-log),
`apps.py`, `checks.py` (route via catalog; gate; composition + `Tags.database`
central-extension checks) · `pg_cron/migrations/0001` (drop `CreateExtension`) ·
`flush.py` (scoped to live db name) · `queues.py` / `connection.py` (central-connection
helper + **psycopg→Django error translation**, B1) · `backends.py`
(`CRON_DATABASE_NAME`, `PG_CRON_ON_TEST_DB` in AbsurdBackendOptions) ·
`pytest_plugin.py` (`absurd_load_schedules` fixture, #101, must commit) ·
`tests/pg_cron/*` (run on an ordinary test DB now; convert emission tests to
`transaction=True`) + new inert-mode tests on plain `db` · `Dockerfile.pg_cron` /
`compose.yaml` (central `cron.database_name`, e.g. `postgres`; `CREATE EXTENSION` +
grants via `/docker-entrypoint-initdb.d` — runs only on a FRESH volume, so existing dev
volumes need recreation) · `docs/WHY.md` (reverse the extension section) ·
`django_absurd/AGENTS.md` + `docs/web/cron-jobs.md` (operator setup: central DB +
grants + one-scheduling-role) · `CLAUDE.md` (the `--create-db` eviction dance

- "test DB must equal cron.database_name" doctrine become obsolete) ·
  `.claude/skills/pg-cron` (update the "In this repo" section) · follow-up: simplify the
  pg_cron example (drop `CRON_DATABASE_NAME`, run on plain `db`).
