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
  `cron.schedule` / `cron.unschedule` remain **PUBLIC-EXECUTE** (only
  `schedule_in_database` and `alter_job` are revoked), so USAGE + the one grant is
  genuinely complete — provided the seam uses `schedule_in_database` (not bare
  `cron.schedule`) everywhere, including the grammar probe (`validators.py:96`), keeping
  it inside the granted set.

- `DROP DATABASE` on a job's target succeeds (launcher holds no session on non-metadata
  DBs) → the `--create-db` eviction hack disappears for app test DBs.
- **End-to-end scenario PROVEN live, airtight** (pg_cron example, central
  `cron.database_name=postgres`, `CreateExtension` temporarily removed, the drain test
  given a `sleep(5)` window): a 1-second `pingpong` on the main `demo` DB fired **35×,
  all `succeeded`, all `database=demo`** during a real suite run in which `test_demo`
  did genuine committed work (enqueue `ping` → wait 5s → `absurd_drain_queue` → task
  `SUCCESSFUL`). `pingpong` **never** touched `test_demo` (no rows, no table), the
  schedule **survived** the run + auto-cleanup, and `test_demo` migrated with NO
  `CREATE EXTENSION`. The whole thesis, confirmed.
- **Clean isolation test PROVEN** (the definitive form): a cron on the main `demo` DB
  that ENQUEUES a real Absurd `ping` task every second accumulated **46 tasks in
  `demo`'s queue**, while a test that drained a **real, migrated `test_demo` queue**
  (without enqueuing anything) found **0** — `before == after == 0`, the drain grabbed
  nothing. A main-DB schedule producing genuine tasks leaks **zero** into the test DB's
  queue. **This becomes the shipped regression test — in `tests/pg_cron/` (the lib
  suite), as its own isolated test**, not the example.

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
to the central name. Central name = auto-discovered via
`current_setting('cron.database_name', true)` on the app connection (NULL → a clear
`ImproperlyConfigured`: server has no pg_cron). **No `CRON_DATABASE_NAME` override
option** — pg_cron is server-local, so the auto-discovered value is definitionally the
only correct one; an override could only equal it or misconfigure. Same-server is
correct by construction (pg_cron is server-local). Scheduling ops are rare (migrate
reconcile, admin save, teardown) so per-op connect cost is fine. The exact pattern
already runs green twice here (`worker.py:171-175` async — pops `cursor_factory`;
`tests/utils.py:72-73` sync). **Commit discipline:** raw psycopg defaults
`autocommit=False` — every write op MUST commit and the grammar probe MUST roll back;
use `autocommit=True` + an explicit per-op `conn.transaction()`, or a forgotten commit
is a silent no-schedule bug. `get_connection_params()` carries SSL `OPTIONS` through
(sslmode inherited); a dbname-routing pooler (pgbouncer) or an `OPTIONS["service"]`
setup needs the central DB reachable — one docs line.

**Homes:** the central-connection open-helper (params-swap + autocommit + error-wrap)
lives in `connection.py` (alongside `resolve_absurd_database`); the `cron.*` seam that
consumes it lives in `catalog.py` (§one-seam). The seam wraps every op in the app
alias's `wrap_database_errors` (see B1) so psycopg errors surface as `django.db.utils.*`
with `__cause__` set — `prune`'s `sqlstate == "XX000"` match depends on running inside
that wrapper.

### One seam for all `cron.*`

Route all current `cron.*` sites (`models.py` schedule/unschedule + the manager's write
methods; `reconcile.py` cleanup-job (un)schedule; `validators` grammar probe;
`flush.drop_pg_cron_state`) through **one new module** (`pg_cron/catalog.py`) exposing a
small verb set (`schedule_job`, `unschedule_job`, `unschedule_jobs_for_database`,
`prune_jobs`, `probe_cron_grammar`, `flush_database_jobs`) that opens the central
connection. NO read verbs ship (the old `PgCronManager` reads had zero prod consumers —
tests read `cron.job` via a `utils.py` helper), and NO dedicated cleanup verbs (the
generic pair covers it). The one remaining test-gate lives here — one gate, not five.

**Error contract (BLOCKING — B1):** a raw psycopg connection raises `psycopg.*`, NOT
Django's `django.db.utils.*` wrappers (Django only wraps errors on its own registered
connections). `catalog.py` MUST translate psycopg exceptions into the Django hierarchy
at the seam boundary — otherwise the existing best-effort catch nets miss them and
crash: `apps.py:103-114` (migrate-never-breaks → an unreachable central DB crashes
migrate), `flush.py:37` (test teardown → every transaction test errors in
`_post_teardown`), `validators.py:100` (grammar probe → a 500 in the admin instead of a
form error). **Mechanism:** wrap raw-psycopg execution in
`connections[<app alias>].wrap_database_errors` (Django's `DatabaseErrorWrapper`) — it
re-raises `psycopg.*` as the matching `django.db.utils.*` with `__cause__` set to the
original psycopg error (the wrapper reads the class map off the Django connection
OBJECT, not the live connection, so a different-dbname central conn is fine).
Load-bearing: `prune_pg_cron_jobs` matches
`getattr(exc.__cause__, "sqlstate", None) == "XX000"`, so the prune/savepoint logic MUST
run inside that wrapper to keep `__cause__.sqlstate`.

### Jobname namespacing (mandatory, defeats upsert-steal)

`_dj:<target_db>:<source>:<name>`, built by ONE constructor
`build_jobname(database, source, name="")` (with `name=""` → the `starts_with` prefix;
the old `build_jobname_prefix` is deleted). The builder MOVES from `validators.py` to
`catalog.py` — it's a name constructor, not a validator, and the seam is its only
caller. The `<db>` is always **implicit**: the seam resolves the LIVE app DB name once
(from the connection) and no public caller passes it. Teardown double-scoped:
`WHERE database = %s AND starts_with(jobname, '_dj:' || %s || ':')` — use `starts_with`,
NOT `LIKE` (`_` is a LIKE wildcard and appears in every test DB name like `test_x_gw1`;
the existing code already uses `starts_with`). Collision-free across xdist workers,
multiple projects, and test-vs-prod on one server. `cron.job.jobname` is untruncated
`text` with **no length limit** — LIVE-VALIDATED: a 300-char jobname round-trips intact
through `schedule_in_database` (no truncation, no NAMEDATALEN 63-byte cut). So the
existing 63-byte `validate_jobname_length` guard is **DELETED** (validator + its model
`clean` call + the `checks.py` `E007_HINT_PG_CRON_JOBNAME` + check branch) — there is no
length restriction to enforce. `validate_name_charset` stays (the `[A-Za-z0-9_-]`
charset guard is still needed since `:` is the jobname separator).

### Emission timing (a real regression to manage) — and NO lock

Today `post_save` schedules on the row's own connection under `pg_advisory_xact_lock`
(`open_locked_cursor`) → row upserts AND cron writes are one atomic, serialized
transaction. Central = a second connection, so emission happens after commit (via
**`transaction.on_commit`**). **The advisory lock is DELETED, with no successor** — it
protected a same-DB atomicity that the two-connection split already breaks, and nothing
left needs serializing: `schedule_in_database` is an idempotent upsert on
`(jobname, username)` (concurrent writers converge, last cadence wins), row upserts race
survivably (`update_or_create` handles the IntegrityError), out-of-commit-order emission
self-heals at the next reconcile. Worse, the central connection is `autocommit=True`, so
a per-statement `pg_advisory_xact_lock` would release instantly — it would guard nothing
without extra explicit-transaction machinery. So: no lock; `open_locked_cursor` is
removed.

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

**Reconcile control flow (both write paths route through one central-conn body).** Save
AND delete signals each register a `transaction.on_commit` callback that opens the
central connection (§central-connection) and runs the catalog op for that row: save →
`schedule_in_database` upsert (db-namespaced jobname); delete → scoped `unschedule` of
that row's job. The bulk reconcilers (`sync_settings_crons`, `sync_admin_crons`, the
`absurd_sync_crons` command) run the SAME central-conn body once: on that one connection
— (1) upsert every declared row's job, (2) prune jobs for rows that no longer exist
(source+db-scoped `WHERE database=ours`), (3) schedule/unschedule the cleanup job via
the generic `schedule_job`/`unschedule_job` (no dedicated cleanup verbs). The per-row
on_commit path and the bulk path share the catalog seam; the difference is only scope
(one row vs all). No lock (see above) — concurrent writers are idempotent + self-heal.

### Cleanup job

The cleanup job moves central too — scheduled with the **generic** `schedule_job` (no
dedicated `reconcile_cleanup_job`/`unschedule_cleanup_job` verbs): a cleanup lane
`source="c"` gives jobname `_dj:<db>:c:cleanup_all`, `command=CLEANUP_COMMAND`,
`database => app_db` (`absurd.cleanup_all_queues` runs in the app DB); `reconcile.py`
keeps the present-or-not (`OPTIONS["CLEANUP"]`) decision and calls `schedule_job` /
`unschedule_job`. The `absurd_cleanup_all` shared identity becomes db-namespaced —
deliberately breaking the shared name with `absurd.enable_cron` / `absurdctl cron`,
acceptable because under central topology those same-DB functions can't run in the app
DB anyway. Absurd-native partition/detach jobs stay unsurfaced (status quo).

### Cleanup lifecycle: teardown sweep AND session-start sweep (both required)

Test-created schedules (opt-in) live as `cron.job` rows in the SHARED central catalog,
target-bound to the test DB. Two sweeps — same operation
(`unschedule WHERE database = <this test DB name> AND starts_with(jobname, '_dj:')` —
`starts_with`, NEVER `LIKE` (`_` is a LIKE wildcard), through the catalog seam as the
test's role; RLS scopes to it), at two different times:

1. **Per-test teardown** — via the existing `_post_teardown` auto-cleanup hook, after
   every committing test. For cross-function isolation: an opt-in test's schedule must
   not bleed into the next test on the reused DB.
2. **Session/worker START sweep (essential, not optional)** — a **session-scoped,
   autouse (per-xdist-worker) fixture** in `pytest_plugin.py`, depending on
   `django_db_setup` (so the test-DB name is known) and entering
   `django_db_blocker.unblock()` for its DB access; runs ONCE, opening the **central
   connection** (raw psycopg via the catalog seam — NOT the Django ORM), unschedules any
   job targeting this worker's test-DB name. Guard the body with
   `apps.is_installed(PG_CRON_APP_NAME)` BEFORE importing `catalog` (the plugin ships in
   core; the pg_cron app may be absent) → no-op when uninstalled. Load-bearing because
   **test DB names are REUSED** (`--reuse-db` keeps `test_<db>`; `--create-db` recreates
   the same name): a run that **crashes before teardown** orphans a job keyed on that
   name, and the next run's `test_<db>` gets the launcher firing the orphan INTO it —
   the scheduler writes tasks the new run never asked for → cross-run contamination
   (PROVEN live). Teardown-only can't catch this (the crash skipped it); the start sweep
   clears the prior orphan before the new run begins. Optionally also prune own-user
   jobs whose `database NOT IN (SELECT datname FROM pg_database)`.

**Speed:** the inert-by-default gate makes both free in normal runs — inert tests can't
create schedules, so the per-test hook's first action is the in-memory
`pg_cron_inert(alias)` check → NO central connection, NO query, zero round-trips (and
the start-sweep fixture no-ops too). Only opt-in (`PG_CRON_ON_TEST_DB=True`) runs pay —
one start sweep per worker + per-test unschedules — and those are rare and already need
a real pg_cron server. (In opt-in mode the per-test teardown MAY further short-circuit
unless the test actually emitted — a cheap flag — but the gate + rarity likely make that
unnecessary.)

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

**No transition/migration code (alpha, from-scratch).** The jobnames gain a
db-namespace, so in principle an in-place upgrade of a same-DB install would leave the
old `_dj:s:foo` jobs alongside the new `_dj:<db>:s:foo` jobs (double-fire until pruned).
django-absurd is alpha and pre-1.0 — we deliberately do NOT ship migration code for
legacy-scheme jobs. Anyone upgrading an existing alpha install clears the old jobs once
with `absurd_sync_crons --teardown` (or a fresh DB); reconcile then emits only the new
scheme. This drops the previously-planned "transition sweep (B2)."

## Test story — the thin residue

The app/test DB never touches `cron.*`, so most of the old gating is gone. What remains
is a small policy layer (still one gate, in `catalog.py`):

- **Detection leaf** — inert when `test_environment_active() OR is_test_database(alias)`
  (EITHER, **not AND**: `test_environment_active()` — `django.test.utils` sets
  `_TestState.saved_data` in every pytest-django / `manage.py test` run,
  per-xdist-worker, NEVER in a real deploy → prod can't misfire — alone guarantees
  prod-safety; requiring BOTH would go LIVE for a project that pins
  `TEST["NAME"] == NAME`). Guard the private `django.test.utils._TestState.saved_data`
  probe with a loud `install_absurd_cleanup`-style version-check (fail at import if the
  attribute is gone). `is_test_database(alias)` reuses the existing
  `ORIGINAL_DATABASE_NAMES` live-name comparison from `apps.py`.
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

Note there are TWO test-DB gates that coexist: the existing migrate-reconcile gate
(`should_sync_schedules`, keyed on `ORIGINAL_DATABASE_NAMES` +
`SYNC_SCHEDULES_ON_TEST_DB`) AND the new catalog inert-leaf (`PG_CRON_ON_TEST_DB`). "One
gate, not five" refers to the `cron.*`-site collapse into `catalog.py`, NOT the migrate
gate. So the **opt-in pg_cron suite must set BOTH** `SYNC_SCHEDULES_ON_TEST_DB=True` AND
`PG_CRON_ON_TEST_DB=True` (via `tests/pg_cron` settings / `build_pg_cron_tasks`).

### Isolation regression — two guarantees, both always-on (NO deselected tests)

The proven live isolation (§verified facts: main-DB `ping` schedule → 0 leak into
`test_demo`) is covered by two tests, **both run in CI** (no `slow`/deselected tests —
that would break the no-CI-skipped-tests + 100%-patch-coverage rules):

- **Structural scoping** — is `test_flush_scoped` (Task 5), not a separate test:
  schedule a job for THIS DB + a control job in another DB, run the real
  `flush_absurd_state` entrypoint, assert only this DB's job is removed and the control
  survives. Proves jobs are db-target-bound and the sweep is correctly scoped —
  deterministically.
- **Runtime no-leak** — `test_isolation_regression.py`: a task-producing cron bound to a
  NON-test DB (per-worker-unique → xdist-safe), made **deterministic by a positive sync
  point** — poll `cron.job_run_details` until the producer fires into its own DB
  (proving the launcher completed a cycle), THEN assert THIS test DB's queue is `0`. No
  blind sleep, no `slow` marker — it runs every CI pass. Guards against a future revert
  of the `database =>` binding to plain `cron.schedule`.

**Eliminated vs the old approach:** conditional `CreateExtension` + all migration
gating; five-site scattered gating (→ one seam); the mirror hazard is structurally gone
(so the once-planned W-level pg_cron-database warning is no longer needed — it was never
built); the "opt-in stays single-worker" non-goal (live opt-in is xdist-safe via
db-namespaced jobs + `WHERE database` teardown); test-DB juggling + the `--create-db`
eviction procedure.

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

`pg_cron/catalog.py` (NEW seam — owns the single db-namespaced `build_jobname`, the
`cron.*` verbs, and the inert gate) · `pg_cron/detection.py` (NEW leaf:
`is_test_database`

- `test_environment_active` + `is_pg_cron_inert` + the `ORIGINAL_DATABASE_NAMES`
  snapshot) · `pg_cron/models.py` (DELETE `PgCronManager`, `get_pg_cron_job`,
  `PgCronJobRow`, `open_locked_cursor`; `schedule_pg_cron_job`/`unschedule_pg_cron_job`
  delegate to catalog; fold `active` into `schedule_in_database`, DROP `alter_job`) ·
  `reconcile.py` (route via catalog; cleanup via generic
  `schedule_job`/`unschedule_job`; no lock) · `validators.py` (DELETE
  `build_jobname`/`build_jobname_prefix` — moved to catalog — and
  `validate_jobname_length`) · `signals.py` (contract rewrite; `on_commit`;
  swallow-and-log; no lock) · `apps.py` (snapshot → `detection`) · `checks.py` (route
  via catalog; DELETE the jobname-length check + hint; add composition + `Tags.database`
  central-extension checks) · `pg_cron/migrations/0001` (drop `CreateExtension`) ·
  `flush.py` (scoped to live db name) · `connection.py` (central-connection helper +
  **psycopg→Django error translation**, B1; GUC-only `resolve_cron_database`) ·
  `backends.py` (`PG_CRON_ON_TEST_DB` in AbsurdBackendOptions — NO `CRON_DATABASE_NAME`)
  · `management/commands/absurd_sync_crons.py` (route via catalog; `CommandError` when
  inert)

* `tests/pg_cron/test_absurd_sync_crons_command.py` (assert inert `CommandError` + live
  sync) · `pytest_plugin.py` (`absurd_load_schedules` fixture, #101, must commit; + the
  **session-scoped autouse start-sweep fixture**) · `tests/pg_cron/*` (run on an
  ordinary test DB now; convert emission tests to `transaction=True`;
  `utils.fetch_cron_job` is the one test-side reader) + new inert-mode tests on plain
  `db` + **the isolation regression: structural via `test_flush_scoped` (Task 5) +
  runtime `test_isolation_regression.py`, BOTH always-on, no `slow`/deselected tests** ·
  `Dockerfile.pg_cron` / `compose.yaml` (central `cron.database_name`, e.g. `postgres`;
  `CREATE EXTENSION` + grants via `/docker-entrypoint-initdb.d` — runs only on a FRESH
  volume, so existing dev volumes need recreation) · `docs/WHY.md` (reverse the
  extension section) · `django_absurd/AGENTS.md`
  - `docs/web/cron-jobs.md` (operator setup: central DB + grants + one-scheduling-role)
    · `CLAUDE.md` (the `--create-db` eviction dance + "test DB must equal
    cron.database_name" doctrine become obsolete) · `.claude/skills/pg-cron` (update the
    "In this repo" section) · follow-up: simplify the pg_cron example (run on plain
    `db`).

## Suggested task order (dependency-safe, for writing-plans)

1. Detection leaf + inert gate + central-connection open-helper (`connection.py`) + B1
   error-wrap.
2. Db-namespaced jobnames + DELETE the `validate_jobname_length` guard/check/hint
   (jobname is unbounded `text`, live-validated).
3. Route all 11 `cron.*` sites through the `catalog.py` seam
   (models/reconcile/validators/ flush/checks/command).
4. `on_commit` emission + reconcile rework (both write paths through one central-conn
   body).
5. Migration drop-`CreateExtension` + `Dockerfile.pg_cron`/`compose.yaml` central
   setup + move `tests/pg_cron` onto an ordinary test DB.
6. System checks (composition + `Tags.database` central-extension).
7. Command inert `CommandError` + teardown/start sweeps + the runtime isolation test
   (structural scoping is already covered by Task 5's `test_flush_scoped`).
8. Docs (WHY reverse, AGENTS/site operator setup, CLAUDE.md, skill).

(No transition sweep — alpha, from-scratch; see §Backward compatibility. No advisory
lock, no `CRON_DATABASE_NAME`, no cleanup/read verbs, no `slow`-deselected tests.)
