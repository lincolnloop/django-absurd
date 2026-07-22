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
is_test_db = (
    connections[backend.database].settings_dict["NAME"]
    != settings.DATABASES[backend.database]["NAME"]
)
key = "SYNC_SCHEDULES_ON_TEST_DB" if is_test_db else "SYNC_SCHEDULES_ON_MIGRATE"
default = False if is_test_db else True
if not backend.options.get(key, default):
    return
```

(`backend.database` is the Django DB **alias** `AbsurdBackend` targets — e.g.
`"default"` — distinct from `alias`, `reconcile_crons_after_migrate`'s existing local
variable for the `TASKS` backend _name_; the two happen to coincide in every example so
far, but are not the same thing and must not be conflated.)

### Test-DB detection — verified against Django's actual source, not assumed

`django.db.backends.base.creation.BaseDatabaseCreation.create_test_db()` mutates
`self.connection.settings_dict["NAME"] = test_database_name`
(`django/db/backends/base/creation.py:73`) — a live mutation of the **connection's**
settings, distinct from the **static** `settings.DATABASES[backend.database]["NAME"]`
declared in the settings module, which is never touched. Comparing the two is therefore
a genuine, Django-native "are we currently on a test DB" signal — no pytest-specific
code, no heuristic on `sys.argv` or env vars, works identically under pytest-django,
`manage.py test`, or any other test runner (including a future `unittest`-based mixin).
Confirmed this holds whether `TEST["NAME"]` is explicitly set (as this project's own
`tests/pg_cron/settings.py` does) or left to Django's auto-generated `test_` prefix —
either way `create_test_db()` mutates the live connection name away from the static one.

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

## Scope

IN:

- The two `OPTIONS` keys, the early-return check in `reconcile_crons_after_migrate`, and
  the test-DB detection helper.
- **System check**: validate each key, if present, is a `bool` — mirroring the existing
  `E006_ENABLE_ADMIN_MSG` pattern in `django_absurd/checks.py` (same `msg`/`hint` split:
  `msg` states the problem, `hint` states the fix).
- **Docs**: both keys documented in `django_absurd/AGENTS.md` (the `OPTIONS` reference
  table) and `docs/web/cron-jobs.md`, explaining the hazard and the safe-by-default
  behavior — mirroring every other public `OPTIONS` key's documentation.
- **Tests**: behavioral, through the real `migrate` entrypoint (matching this project's
  own `tests/pg_cron/test_pg_cron_post_migrate.py::run_cron_sync` fixture pattern, which
  already parametrizes over `absurd_sync_crons`/`migrate` as the two real reconcile
  entrypoints) — not a unit-level call into `reconcile_crons_after_migrate` directly.
  Cover: default behavior on a real (non-test) DB unchanged; default behavior on a test
  DB skips sync; explicit `True`/`False` overrides on both keys; per-row `ScheduledTask`
  create/delete still schedules/ unschedules regardless of either key's value (proving
  the guard is scoped to the bulk reconcile only).

OUT (tracked elsewhere, not this spec):

- `absurd_cron_schedule` (the on-demand CRUD fixture) and the rest of the
  distributable-pytest-fixtures work — `docs/specs/2026-07-21-pytest-plugin-design.md`,
  branch `pytest-plugin`. This feature is a prerequisite for that spec's cron-related
  fixtures, not a replacement for any of it.

## Constraints carried over

`import typing as t`; absolute imports; verb-named functions; no monkeypatching;
behavioral tests via real entrypoints; full patch coverage; complete error-message
assertions; docs mirrored between `AGENTS.md` and `docs/web/`.
