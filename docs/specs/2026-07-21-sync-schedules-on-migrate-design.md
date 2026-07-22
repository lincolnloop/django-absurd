# `SYNC_SCHEDULES_ON_MIGRATE` / `SYNC_SCHEDULES_ON_TEST_DB` — Design

**Goal:** stop `migrate` from silently syncing a project's declared pg_cron schedules
into a real, live `cron.job` catalog whenever it runs against a **test** database — a
real hazard, not hypothetical, confirmed against real infrastructure (see Validation).
Standalone core-library feature; gates the rest of the distributable-pytest-fixtures
work (`docs/specs/2026-07-21-pytest-plugin-design.md`, branch `pytest-plugin`), which
depends on this landing first.

## Problem (confirmed, not theoretical)

`django_absurd/pg_cron/apps.py`'s `PgCronConfig.ready()` wires
`post_migrate.connect(reconcile_crons_after_migrate, sender=self)`. That receiver calls
`sync_crons(backend)` (settings-declared `OPTIONS["SCHEDULE"]`) and `sync_admin_crons()`
(admin-authored `ScheduledTask` rows), unconditionally, every time `migrate` runs.
pytest-django's `--create-db`/first-run test-DB setup runs a real `migrate`. If a
consumer's test settings share the same `TASKS`/`OPTIONS["SCHEDULE"]` as production (a
common pattern — one settings module, or `from base import *`), their real recurring
schedules get synced into the **test** database's `cron.job` the moment the test DB is
created — and pg_cron's launcher (a Postgres background worker, entirely outside
pytest/Django's control) will fire them for real, on schedule, against test data, for
the rest of the session.

## Mechanism

Two new per-backend `OPTIONS` keys, following the existing convention
(`backend.options.get("KEY", default)`, e.g. `DEFAULT_MAX_ATTEMPTS`/`ENABLE_ADMIN`):

- **`SYNC_SCHEDULES_ON_MIGRATE`** (bool, default `True`) — governs `migrate` against a
  **real** (non-test) database. Defaulting `True` preserves today's behavior for every
  existing consumer; no breaking change on upgrade.
- **`SYNC_SCHEDULES_ON_TEST_DB`** (bool, default `False`) — governs `migrate` when
  Django's test framework has swapped in a **test** database. Defaulting `False` closes
  the hazard out of the box, for every consumer, with **zero required settings changes**
  — no separate test-settings module, no manual override needed.

`reconcile_crons_after_migrate` (`django_absurd/pg_cron/apps.py`) checks which one
applies and returns early (skipping both `sync_crons()` and `sync_admin_crons()`) if the
applicable key is `False`:

```
is_test_db = connections[backend.database].settings_dict["NAME"] != ORIGINAL_DATABASE_NAMES.get(backend.database)
key = "SYNC_SCHEDULES_ON_TEST_DB" if is_test_db else "SYNC_SCHEDULES_ON_MIGRATE"
default = False if is_test_db else True
if not backend.options.get(key, default):
    return
```

`ORIGINAL_DATABASE_NAMES` is a module-level `dict[str, str]` in
`django_absurd/pg_cron/apps.py`, populated once, inside `PgCronConfig.ready()`, by
copying each configured alias's `NAME` value **out of** `settings.DATABASES` at that
moment (a plain string extraction, not a dict reference — see below for why this
distinction is load-bearing).

(`backend.database` is the Django DB **alias** `AbsurdBackend` targets — e.g.
`"default"` — distinct from `alias`, `reconcile_crons_after_migrate`'s existing local
variable for the `TASKS` backend _name_; the two happen to coincide in every example so
far, but are not the same thing and must not be conflated.)

**Why here, specifically — not in `sync_crons`/`sync_admin_crons`, not at
`ready()`-time:** `django_absurd/pg_cron/management/commands/absurd_sync_crons.py`'s
`handle()` calls `sync_crons(backend)` / `sync_admin_crons()` **directly** — a separate
call site from `reconcile_crons_after_migrate`. Putting the guard in the shared
functions instead of the receiver would also silently suppress an explicit, deliberate
`manage.py absurd_sync_crons` run against a test DB — unwanted; a user who types that
command wants it to actually sync, regardless of which DB they're pointed at. And the
check can't move any further upstream to `PgCronConfig.ready()`'s
`post_migrate.connect(...)` call either — `ready()` runs once at process start, before
any test framework has swapped in a test DB, so it can't yet know which case a _future_
migrate invocation will hit. The receiver is the only point that is both
migrate-specific and running late enough to see the swapped connection.

### Test-DB detection — verified twice against real infrastructure, second attempt fixed a real bug in the first

**First attempt (wrong — do not use):** comparing
`connections[alias].settings_dict["NAME"]` against `settings.DATABASES[alias]["NAME"]`
directly. This looked sound from reading `django/db/backends/base/creation.py:73` alone
(`create_test_db()` does `self.connection.settings_dict["NAME"] = test_database_name`),
but verified empirically — twice, once bare and once inside a real pytest-django test
against the real `db_pg_cron` container — that `connections[alias].settings_dict` **is
`settings.DATABASES[alias]` — the same dict object**, not a copy
(`django/utils/connection.py`'s `BaseConnectionHandler.settings` returns
`django_settings.DATABASES` directly; `ConnectionHandler.create_connection` does
`db = self.settings[alias]; backend.DatabaseWrapper(db, alias)`, no copy anywhere). So
`create_test_db()`'s mutation updates **both** "views" simultaneously — they can never
differ, and this comparison always evaluates `False`, silently defeating the whole
feature.

**Fix, also verified empirically:** since `PgCronConfig.ready()` always runs before any
test-DB swap (established above — `ready()` fires during `django.setup()`, strictly
before pytest-django's `django_db_setup` fixture or `manage.py test`'s
`setup_databases()`, in every real flow), snapshot each alias's `NAME` **value** (a
plain string extraction — copying the value out, not holding a reference to the dict)
into a module-level `ORIGINAL_DATABASE_NAMES` dict inside `ready()`, then compare the
_live_ value against that frozen snapshot later, at signal-fire time. Verified directly:
a value captured at collection time (same timing guarantee as `ready()`) read
`"postgres"`; the live value inside a real, running `pytest.mark.django_db` test — after
pytest-django's real test-DB creation had already run — read `"absurd_test_pg_cron"`.
Genuine divergence, correctly detected. No pytest-specific code, no heuristic on
`sys.argv` or env vars — works identically under pytest-django, `manage.py test`, or any
other test runner (including a future `unittest`-based mixin).

## Validation (already done — a real, throwaway prototype, not a proof sketch)

Built and ran a standalone script against the real `db_pg_cron` test container
(`tests.pg_cron.settings`, real `cron` extension, real `migrate`), proving three things
end to end:

1. **Unpatched**: declaring
   `OPTIONS["SCHEDULE"] = {"prod_report": {"task": ..., "cron": "* * * * *"}}` and
   running plain `migrate` produced a real, `active=True` `cron.job` row
   (`_dj:s:prod_report`) — the hazard reproduces for real.
2. **With the guard mechanism applied** (prototyped via a `post_migrate.disconnect(...)`
   stand-in, since the actual `OPTIONS`-key implementation didn't exist yet): `migrate`
   ran clean, zero `cron.job` rows created.
3. **Per-row signals stay fully intact regardless**: creating one `ScheduledTask` row
   directly via the ORM still produced a real, live `cron.job` row immediately (via the
   untouched `post_save` → `schedule_job_on_save` signal); deleting it removed the job
   (via the untouched `post_delete` → `unschedule_job_on_delete` signal). This is the
   path the deferred `absurd_cron_schedule` fixture (tracked in the pytest-plugin spec)
   will build on — no new scheduling logic needed there at all.

Also confirmed along the way: a plain `settings.TASKS = ...` assignment does **not**
take effect for `AbsurdBackend` resolution — Django's `task_backends`
(`TaskBackendHandler`, a `BaseConnectionHandler` like `django.db.connections`) caches
backends per-alias and only invalidates on the `setting_changed` signal, which
`override_settings(...).enable()` sends and a bare attribute assignment doesn't. Not
directly relevant to this feature's production code, but worth remembering for this
feature's own tests (must use the `settings` fixture / `override_settings`, never a bare
assignment, to actually exercise a changed `OPTIONS["SCHEDULE"]`).

**Second round, after this design's first revdiff pass:** the originally-proposed
test-DB detection (live-vs-static dict comparison) was re-verified against the real
`db_pg_cron` container and found broken — see "Test-DB detection" above for the full
account. The corrected snapshot-based mechanism was verified in its place, both bare and
inside a real running `pytest.mark.django_db` test.

## Scope

IN:

- The two `OPTIONS` keys, the `ORIGINAL_DATABASE_NAMES` snapshot populated in
  `PgCronConfig.ready()`, and the early-return check in `reconcile_crons_after_migrate`.
- **Keep this project's own pg_cron suite green.**
  `tests/pg_cron/test_pg_cron_post_migrate.py`'s `run_cron_sync` fixture parametrizes
  over `["absurd_sync_crons", "migrate"]` and asserts **identical** outcomes for both —
  which `SYNC_SCHEDULES_ON_TEST_DB=False` by default would break, since every test in
  this suite runs on a test DB and the two entrypoints would now diverge
  (`absurd_sync_crons` still syncs; `migrate` doesn't). Fix:
  `tests/pg_cron/utils.py::build_pg_cron_tasks` sets
  `OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"] = True` by default in what it returns —
  restoring today's behavior for this project's own suite (which is specifically testing
  reconcile-on-migrate) without touching the many individual call sites.
- **Docs**: both keys documented in `django_absurd/AGENTS.md` (the `OPTIONS` reference
  table) and `docs/web/cron-jobs.md`, explaining the hazard and the safe-by-default
  behavior — mirroring every other public `OPTIONS` key's documentation.
- **Tests**: behavioral, through real entrypoints, in a new dedicated file
  (`tests/pg_cron/test_sync_schedules_on_migrate.py` — not added to
  `test_pg_cron_post_migrate.py`, since `run_cron_sync` exists specifically to prove the
  two entrypoints are _identical_, the opposite of what these tests show). Two-pronged,
  matching how each branch was actually validated:
  - **Test-DB branch** (`SYNC_SCHEDULES_ON_TEST_DB`): in-process, via the normal
    pytest-django test DB (no simulation needed — every test in this suite already runs
    on one). Cover: default (`False`) skips sync; explicit `True` override syncs;
    per-row `ScheduledTask` create/delete still schedules/unschedules regardless of
    either key's value (proving the guard is scoped to the bulk reconcile only, not the
    per-row signals).
  - **Real (non-test) DB branch** (`SYNC_SCHEDULES_ON_MIGRATE`): a genuinely separate
    database on the same `db_pg_cron` container — never touched by pytest-django's
    test-DB machinery at all — with `manage.py migrate` run via subprocess against it,
    and real create/drop bracketing the test. Higher-fidelity than any in-process
    simulation, and independently validates the snapshot mechanism: since no swap ever
    happens here, `ORIGINAL_DATABASE_NAMES` and the live value naturally stay equal,
    correctly evaluating `is_test_db = False`.

OUT (tracked elsewhere, not this spec):

- `absurd_cron_schedule` (the on-demand CRUD fixture) and the rest of the
  distributable-pytest-fixtures work — `docs/specs/2026-07-21-pytest-plugin-design.md`,
  branch `pytest-plugin`. This feature is a prerequisite for that spec's cron-related
  fixtures, not a replacement for any of it.

## Constraints carried over

`import typing as t`; absolute imports; verb-named functions; no monkeypatching;
behavioral tests via real entrypoints; full patch coverage; complete error-message
assertions; docs mirrored between `AGENTS.md` and `docs/web/`.
