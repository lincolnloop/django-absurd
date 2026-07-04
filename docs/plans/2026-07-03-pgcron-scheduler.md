# pg_cron scheduler (SP2) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement task-by-task. Steps use checkbox (`- [ ]`) syntax. **Impl steps are
> prose, not code** (per project TDD rule: tests RED-first, minimal implementation
> described in prose — never a finished solution block). Test steps show real test code.

**Goal:** DB-side execution of settings-declared recurring tasks via `pg_cron`, selected
by `OPTIONS["SCHEDULER"]="pg_cron"`.

**Architecture:** Reconcile materializes each declared `SCHEDULE` entry into (a) a
`ScheduledJob` row and (b) a `pg_cron` job whose command is the **constant**
`select django_absurd_run_scheduled('<name>')`; a search-path-safe wrapper function
reads the row at fire time and calls `absurd.spawn_task`. Task data never enters SQL
text (no injection surface). `post_migrate` + `absurd_sync_crons` drive reconcile;
static `E007` validates; beat and `pg_cron` are mutually exclusive.

**Tech stack:** Django 6, psycopg3, `pg_cron` ≥ 1.4, `croniter`, absurd_sdk, pytest.

**Source spec:** `docs/specs/2026-07-03-pgcron-scheduler-design.md` (read it; this plan
implements it verbatim).

## Global Constraints

- Django ≥ 6.0, Python ≥ 3.12, psycopg (v3) backend only.
- `pg_cron` ≥ 1.4 required at runtime (`cron.alter_job`). django-absurd ships NO
  `CREATE EXTENSION pg_cron` migration — install is an operator prerequisite.
- Topology **T1 only**: `pg_cron` co-located with Absurd on the backend's `DATABASE`.
- Settings-declared source of truth; the projection table/reconcile are machine-owned
  (`source="settings"`); `source="admin"` reserved for SP3 and never touched by this SP.
- Job naming `absurd:settings:<alias>:<name>`; composed jobname ≤ 63 bytes; `name` and
  `alias` charset-restricted.
- Sub-minute is beat-only; `pg_cron` backend rejects 6-field cron.
- Reconcile: advisory-lock serialized; prune/teardown per-job savepoint swallowing the
  not-found error (`InternalError` / SQLSTATE `XX000`, NOT `ProgrammingError`); no raw
  `DELETE FROM cron.job` (no grant).
- Spawn parity: `max_attempts` falls back to `backend.default_max_attempts` (NOT literal
  5); reshape via the SDK's imported `_normalize_spawn_options`; never route through
  `client.spawn`. No `idempotency_key` on the `pg_cron` path.
- Wrapper function `SET search_path = pg_catalog`, every object fully schema-qualified.
- `manage.py check` stays DB-free (E007 static); `pg_cron` facts validated at sync.
- Testing: function-based pytest; behavioral via command/`check`, assert full emitted
  text; no monkeypatch/`unittest.mock`; `import typing as t`; absolute imports; verb
  names; helpers below callers; hold 100% patch coverage on changed lines.

## File structure

- `django_absurd/pgcron.py` (**new**) — the reconcile seam: `sync_crons`,
  `teardown_crons`, `resolve_spawn_options`, `effective_queue`, `build_jobname`,
  `build_command`, advisory-lock constant. All `pg_cron` SQL confined here. (`Schedule`
  and `get_settings_schedules` stay in `scheduler.py`, shared with beat.)
- `django_absurd/models.py` (**modify**) — add managed `ScheduledJob`.
- `django_absurd/migrations/0002_scheduledjob.py` (**new**) — the model.
- `django_absurd/migrations/0003_run_scheduled_function.py` (**new**) — `RunSQL` wrapper
  function (+ reverse drop).
- `django_absurd/backends.py` (**modify**) — read `OPTIONS["SCHEDULER"]` (default
  `"beat"`); expose `backend.scheduler`; add `SCHEDULER`/`SCHEDULE` to the options
  TypedDict.
- `django_absurd/checks.py` (**modify**) — extend `validate_schedule` (SCHEDULER-aware:
  6-field reject, name/jobname charset+length, effective-queue) + SCHEDULER-value check.
- `django_absurd/management/commands/absurd_sync_crons.py` (**new**).
- `django_absurd/management/commands/absurd_beat.py` + `absurd_worker.py` (**modify**) —
  refuse under `SCHEDULER="pg_cron"`.
- `django_absurd/apps.py` (**modify**) — `post_migrate` reconcile after provision.
- **Test infra**: `Dockerfile.pgcron` (or `docker/`), `compose.yaml`,
  `tests/settings.py` (fixed TEST NAME), `pyproject.toml` (`pgcron` marker), a CI job.
- **Docs/example**: `docs/web/cron-jobs.md`, `django_absurd/AGENTS.md`, `examples/*`,
  `docs/WHY.md`.

---

### Task 1: pg_cron test infrastructure

Everything behavioral needs a real `pg_cron`. `CREATE EXTENSION pg_cron` is allowed only
in `cron.database_name`; the suite must run against a DB whose name matches that GUC.

**Files:**

- Create: `Dockerfile.pgcron`, `docker/initdb-pgcron.sh`
- Modify: `compose.yaml`, `tests/settings.py`, `pyproject.toml`, `tests/conftest.py`,
  `.github/workflows/*` (a `pgcron` job)
- Test: `tests/test_pgcron_infra.py`

**Interfaces:**

- Produces: a `@pytest.mark.pgcron` marker; a `pgcron_conn`/fixture or reuse of the
  Django default connection pointed at a `pg_cron`-enabled DB whose name ==
  `cron.database_name`.

- [ ] **Step 1: Failing test** — `tests/test_pgcron_infra.py`:

```python
import pytest
from django.db import connection

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pgcron]


def test_pgcron_extension_available():
    with connection.cursor() as cur:
        cur.execute("select extversion from pg_extension where extname = 'pg_cron'")
        row = cur.fetchone()
    assert row is not None, "pg_cron extension not installed on the test DB"
    major, minor = (int(p) for p in row[0].split(".")[:2])
    assert (major, minor) >= (1, 4), f"pg_cron {row[0]} < 1.4"


def test_can_schedule_and_unschedule():
    with connection.cursor() as cur:
        cur.execute("savepoint p")
        cur.execute(
            "select cron.schedule(%s, %s, %s)",
            ["absurd:__probe__", "* * * * *", "select 1"],
        )
        jobid = cur.fetchone()[0]
        cur.execute("select cron.unschedule(%s)", [jobid])
        cur.execute("rollback to savepoint p")
    assert jobid is not None
```

- [ ] **Step 2: Run — expect FAIL** (extension absent on `postgres:18-alpine`):
      `uv run pytest tests/test_pgcron_infra.py -m pgcron -v` → FAIL / error (no
      `pg_cron`).

- [ ] **Step 3: Build the image + wire compose (prose).** Add `Dockerfile.pgcron`:
      `FROM postgres:18` then install the matching `postgresql-18-cron` package
      (Debian-based `postgres:18`, not alpine — alpine has no pg_cron package). Add
      `docker/initdb-pgcron.sh` (runs at container init as superuser) doing
      `CREATE EXTENSION IF NOT EXISTS pg_cron;`. In `compose.yaml`: build the db from
      `Dockerfile.pgcron`; mount the initdb script into `/docker-entrypoint-initdb.d/`;
      set
      `command: postgres -c shared_preload_libraries=pg_cron -c cron.database_name=<TESTDB>`.
      Pick a fixed `<TESTDB>` (e.g. `absurd_test`).

- [ ] **Step 4: Fix the test DB name (prose).** In `tests/settings.py` set
      `DATABASES["default"]["TEST"] = {"NAME": "<TESTDB>"}` so pytest-django uses the DB
      whose name matches `cron.database_name` (else `CREATE EXTENSION`/`cron.schedule`
      won't be in the right DB). Register the `pgcron` marker in `pyproject.toml`
      `[tool.pytest.ini_options]` `markers` (mirror the existing `packaging` marker).

- [ ] **Step 5: CI (prose).** Add a dedicated CI job (or extend the DB service) that
      brings up the `Dockerfile.pgcron` compose service and runs
      `uv run pytest -m pgcron`. The existing matrix runs `-m "not pgcron"` (mirror the
      `packaging` exclusion in `tox.ini`). Document that local runs need
      `docker compose up -d db` on the new image.

- [ ] **Step 6: Run — expect PASS.**
      `docker compose up -d --build db && uv run pytest tests/test_pgcron_infra.py -m pgcron -v`
      → 2 passed.

- [ ] **Step 7: Commit** —
      `chore(test): pg_cron-enabled Postgres image + pgcron marker`.

---

### Task 2: `ScheduledJob` model + migration

**Files:**

- Modify: `django_absurd/models.py`
- Create: `django_absurd/migrations/0002_scheduledjob.py`
- Test: `tests/test_scheduledjob_model.py`

**Interfaces:**

- Produces: `django_absurd.models.ScheduledJob` with fields `name`, `source` (default
  `"settings"`), `alias`, `task`, `queue` (nullable), `params` (JSONField), `options`
  (JSONField), `cron`, `enabled` (default `True`), `created_at`, `updated_at`;
  `Meta.unique_together = (("source", "alias", "name"),)`; table
  `django_absurd_scheduledjob`.

- [ ] **Step 1: Failing test** — `tests/test_scheduledjob_model.py`:

```python
import pytest
from django.db import IntegrityError

from django_absurd.models import ScheduledJob

pytestmark = pytest.mark.django_db(transaction=True)


def test_roundtrip_defaults():
    job = ScheduledJob.objects.create(
        name="nightly", alias="default", task="tests.tasks.add",
        params={"args": [], "kwargs": {}}, options={}, cron="0 2 * * *",
    )
    assert job.source == "settings"
    assert job.enabled is True


def test_unique_per_source_alias_name():
    kw = dict(alias="default", task="tests.tasks.add",
              params={"args": [], "kwargs": {}}, options={}, cron="0 2 * * *")
    ScheduledJob.objects.create(name="dup", source="settings", **kw)
    ScheduledJob.objects.create(name="dup", source="admin", **kw)  # other source OK
    with pytest.raises(IntegrityError):
        ScheduledJob.objects.create(name="dup", source="settings", **kw)
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: ScheduledJob`):
      `uv run pytest tests/test_scheduledjob_model.py -v`.

- [ ] **Step 3: Add the model (prose).** In `models.py` add a managed `ScheduledJob`
      model with the fields/Meta in Interfaces. Add it to `__all__`. `source` uses
      `TextChoices` (`SETTINGS="settings"`, `ADMIN="admin"`). `params`/`options` are
      `JSONField` (default `dict`). Keep it below the existing models.

- [ ] **Step 4: Generate the migration (prose).**
      `uv run python -m django makemigrations django_absurd --name scheduledjob` (via
      the test settings module). Verify it creates `0002_scheduledjob.py` and applies
      cleanly. Do NOT hand-edit unless makemigrations misnames it.

- [ ] **Step 5: Run — expect PASS.**
      `uv run pytest tests/test_scheduledjob_model.py -v`.

- [ ] **Step 6: Commit** — `feat(scheduler): add ScheduledJob projection model`.

---

### Task 3: wrapper function `django_absurd_run_scheduled`

**Files:**

- Create: `django_absurd/migrations/0003_run_scheduled_function.py`
- Test: `tests/test_run_scheduled_fn.py`

**Interfaces:**

- Produces: SQL function `public.django_absurd_run_scheduled(p_name text) returns void`,
  `SET search_path = pg_catalog`, security INVOKER, that reads
  `public.django_absurd_scheduledjob` by `name` (any source) and, when the row exists
  and `enabled`, runs
  `select absurd.spawn_task(queue, task, params, coalesce(options, '{}'::jsonb))`;
  otherwise no-op.

- [ ] **Step 1: Failing test** — `tests/test_run_scheduled_fn.py`:

```python
import pytest
from django.db import connection

from django_absurd.models import ScheduledJob
from tests.models import Payload

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pgcron]


def _run(name):
    with connection.cursor() as cur:
        cur.execute("select public.django_absurd_run_scheduled(%s)", [name])


def test_fires_task_from_row():
    ScheduledJob.objects.create(
        name="p", alias="default", task="tests.tasks.create_payload",
        queue="default", params={"args": ["tick"], "kwargs": {}}, options={},
        cron="* * * * *",
    )
    _run("p")
    from django.core.management import call_command
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 1


def test_missing_row_is_noop():
    _run("nope")  # no exception


def test_disabled_row_is_noop():
    ScheduledJob.objects.create(
        name="off", alias="default", task="tests.tasks.create_payload",
        queue="default", params={"args": ["x"], "kwargs": {}}, options={},
        cron="* * * * *", enabled=False,
    )
    _run("off")
    from django.core.management import call_command
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 0
```

- [ ] **Step 2: Run — expect FAIL** (function does not exist).

- [ ] **Step 3: Write the RunSQL migration (prose).** New migration depending on
      `0002_scheduledjob` AND the Absurd-schema migration (`0001_initial_0_4_0`, so
      `absurd.spawn_task` exists). `migrations.RunSQL` with forward =
      `CREATE OR REPLACE FUNCTION public.django_absurd_run_scheduled(p_name text) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$ ... $$`
      — body selects the row from `public.django_absurd_scheduledjob`, `RETURN` when not
      found or not `enabled`, else
      `PERFORM absurd.spawn_task(v.queue, v.task, v.params, coalesce(v.options, '{}'::jsonb))`.
      Reverse = `DROP FUNCTION IF EXISTS public.django_absurd_run_scheduled(text)`.
      Every identifier schema-qualified.

- [ ] **Step 4: Run — expect PASS** (3 tests).

- [ ] **Step 5: Commit** —
      `feat(scheduler): pg_cron wrapper function (search-path-safe)`.

---

### Task 4: `SCHEDULER` option + beat/pg_cron mutual exclusion

**Files:**

- Modify: `django_absurd/backends.py`,
  `django_absurd/management/commands/absurd_beat.py`,
  `django_absurd/management/commands/absurd_worker.py`
- Test: `tests/test_scheduler_selector.py`

**Interfaces:**

- Produces: `backend.scheduler: str` (`"beat"` default | `"pg_cron"`), read from
  `OPTIONS["SCHEDULER"]`. `absurd_beat` and `absurd_worker --beat` raise `CommandError`
  when `backend.scheduler == "pg_cron"`.

- [ ] **Step 1: Failing test** — `tests/test_scheduler_selector.py`:

```python
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def pgcron_tasks(schedule=None):
    return {"default": {"BACKEND": ABSURD, "OPTIONS": {
        "QUEUES": {"default": {}}, "SCHEDULER": "pg_cron",
        "SCHEDULE": schedule or {}}}}


def test_scheduler_defaults_to_beat(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["default"]}}
    assert get_absurd_backends()["default"].scheduler == "beat"


def test_beat_command_refuses_under_pgcron(settings):
    settings.TASKS = pgcron_tasks()
    with pytest.raises(CommandError, match="SCHEDULER is pg_cron"):
        call_command("absurd_beat")


def test_worker_beat_flag_refuses_under_pgcron(settings):
    settings.TASKS = pgcron_tasks()
    with pytest.raises(CommandError, match="SCHEDULER is pg_cron"):
        call_command("absurd_worker", queue="default", beat=True)
```

- [ ] **Step 2: Run — expect FAIL** (`scheduler` attr missing; no refusal).

- [ ] **Step 3: Implement (prose).** In `AbsurdBackend.__init__` read
      `self.scheduler = self.options.get("SCHEDULER", "beat")`; add `SCHEDULER` and
      `SCHEDULE` to `AbsurdBackendOptions`. In `absurd_beat.handle` and
      `absurd_worker.handle` (when `options["beat"]`), raise
      `CommandError("SCHEDULER is pg_cron — beat disabled; run absurd_sync_crons")` if
      `backend.scheduler == "pg_cron"`. Place the check before starting any loop.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** —
      `feat(scheduler): SCHEDULER selector; beat refuses under pg_cron`.

---

### Task 5: `resolve_spawn_options` + `effective_queue`

**Files:**

- Create: `django_absurd/pgcron.py`
- Test: `tests/test_pgcron_options.py`

**Interfaces:**

- Consumes: `Schedule` (scheduler.py), `build_merged_spawn_options` (backends.py),
  `absurd_sdk._normalize_spawn_options`.
- Produces: `resolve_spawn_options(backend, schedule) -> dict` (JSON-ready `p_options`);
  `effective_queue(schedule) -> str`.

- [ ] **Step 1: Failing test** — `tests/test_pgcron_options.py`:

```python
import pytest

from django_absurd.backends import get_absurd_backends
from django_absurd.pgcron import effective_queue, resolve_spawn_options
from django_absurd.scheduler import Schedule

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def backend_with(default_max_attempts=None):
    opts = {"QUEUES": {"default": {}, "reports": {}}}
    if default_max_attempts is not None:
        opts["DEFAULT_MAX_ATTEMPTS"] = default_max_attempts
    from django.test import override_settings
    return opts


def test_max_attempts_from_decorator(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {"default": {}}}}}
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.capped", cron="0 2 * * *")
    assert resolve_spawn_options(be, s)["max_attempts"] == 3  # capped => @absurd_default_params(max_attempts=3)


def test_max_attempts_falls_back_to_backend_default(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "OPTIONS": {
        "QUEUES": {"default": {}}, "DEFAULT_MAX_ATTEMPTS": 7}}}
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.add", cron="0 2 * * *")  # no decorator
    assert resolve_spawn_options(be, s)["max_attempts"] == 7  # NOT 5


def test_effective_queue_uses_task_queue_name_when_unset(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {"default": {}, "reports": {}}}}}
    s = Schedule(name="x", task="tests.tasks.on_reports", cron="0 2 * * *")  # @task(queue_name="reports")
    assert effective_queue(s) == "reports"
```

(Add test fixtures `tests.tasks.capped` =
`@task`/`@absurd_default_params(max_attempts=3)` and `tests.tasks.on_reports` =
`@task(queue_name="reports")` if absent.)

- [ ] **Step 2: Run — expect FAIL** (module/functions missing).

- [ ] **Step 3: Implement (prose).** `effective_queue(schedule)`:
      `schedule.queue or import_string(schedule.task).queue_name`.
      `resolve_spawn_options`: `task = import_string(schedule.task)`;
      `defaults = getattr(task.func, "absurd_default_params", None)`;
      `merged = build_merged_spawn_options(defaults, None)`;
      `merged["max_attempts"] = merged.pop("max_attempts", backend.default_max_attempts)`;
      `return _normalize_spawn_options(**merged)` (imported from `absurd_sdk`, with a
      pin note). Do not set `idempotency_key`. Helpers below the public functions.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** —
      `feat(scheduler): resolve_spawn_options + effective_queue (enqueue parity)`.

---

### Task 6: jobname + command builders

**Files:**

- Modify: `django_absurd/pgcron.py`
- Test: `tests/test_pgcron_naming.py`

**Interfaces:**

- Produces: `build_jobname(alias, name, source="settings") -> str` →
  `f"absurd:{source}:{alias}:{name}"`; `JOBNAME_PREFIX(alias, source) -> str` →
  `f"absurd:{source}:{alias}:"` (for prune LIKE). Command construction lives in
  `sync_crons` (Task 8) as a parameterized statement.

- [ ] **Step 1: Failing test** — `tests/test_pgcron_naming.py`:

```python
from django_absurd.pgcron import build_jobname


def test_jobname_format():
    assert build_jobname("default", "nightly") == "absurd:settings:default:nightly"
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement (prose).** Add the two small helpers; single f-string each.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(scheduler): pg_cron job-name helpers`.

---

### Task 7: E007 static extensions (SCHEDULER-aware)

**Files:**

- Modify: `django_absurd/checks.py`
- Test: `tests/test_pgcron_checks.py`

**Interfaces:**

- Consumes: `build_jobname`, `effective_queue`.
- Produces: extended `absurd.E007` — under `SCHEDULER="pg_cron"`: reject 6-field cron;
  reject `name` not matching `[A-Za-z0-9_-]+`; reject composed jobname > 63 bytes or a
  bad-charset `alias`; validate the **effective** queue is declared. Plus a
  SCHEDULER-value check (unknown value → error). All static (no DB).

- [ ] **Step 1: Failing tests** — `tests/test_pgcron_checks.py` (reuse the `run_check`
      callable-fixture pattern from `tests/test_scheduler_checks.py`; each asserts the
      full emitted `absurd.E007` text):

```python
# cases (one test each), all under OPTIONS["SCHEDULER"]="pg_cron":
# - cron "*/30 * * * * *" (6-field) -> E007 "... minute-granularity; use the beat scheduler ..."
# - name "bad name!" -> E007 "... invalid schedule name ..."
# - alias/name composing > 63 bytes -> E007 "... job name exceeds 63 bytes ..."
# - task @task(queue_name="ghost") with ghost undeclared, no explicit queue -> E007 "... queue 'ghost' is not declared."
# - OPTIONS["SCHEDULER"]="pgcron" (typo) -> E007 "... unknown SCHEDULER 'pgcron' ..."
# - happy 5-field under pg_cron -> no absurd.E007 in output
```

(Write each as a full function driving `call_command("check", "django_absurd")` and
asserting on captured text, matching `test_scheduler_checks.py` style.)

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement (prose).** Thread `backend.scheduler` + `alias` into
      `validate_schedule` (or a sibling `validate_pgcron_schedule` called when scheduler
      is pg_cron). Add: a SCHEDULER-value check in `check_absurd_schedule_config`;
      6-field detection (count fields on the cron string; reject when 6 under pg_cron);
      `name` charset regex; composed-jobname length
      (`len(build_jobname(alias,name).encode()) <= 63`)
  - alias charset; effective-queue-declared (compute `effective_queue`, check membership
    in `declared_queues`). Each emits an `absurd.E007` `Error` with a distinct message +
    hint. Keep beat's path unchanged (still accepts full croniter). No DB access.

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** —
      `feat(checks): E007 pg_cron validation (6-field, name, jobname, queue, SCHEDULER)`.

---

### Task 8: `sync_crons` — projection rows (table phase)

**Files:**

- Modify: `django_absurd/pgcron.py`
- Test: `tests/test_pgcron_sync_rows.py`

**Interfaces:**

- Consumes: `get_settings_schedules`, `resolve_spawn_options`, `effective_queue`,
  `build_jobname`.
- Produces: `sync_crons(backend) -> None` — table phase: advisory-lock, upsert
  `source="settings"` rows for declared entries, delete undeclared settings rows for
  this alias. (pg_cron phase added in Task 9.)

- [ ] **Step 1: Failing test** — `tests/test_pgcron_sync_rows.py`:

```python
import pytest
from django.core.management import call_command  # (or call sync_crons directly)

from django_absurd.backends import get_absurd_backends
from django_absurd.models import ScheduledJob
from django_absurd.pgcron import sync_crons

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pgcron]

ABSURD = "django_absurd.backends.AbsurdBackend"


def tasks(schedule):
    return {"default": {"BACKEND": ABSURD, "OPTIONS": {
        "QUEUES": {"default": {}, "reports": {}}, "SCHEDULER": "pg_cron",
        "SCHEDULE": schedule}}}


def test_upsert_and_prune_settings_rows(settings):
    settings.TASKS = tasks({
        "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
        "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
    })
    be = get_absurd_backends()["default"]
    sync_crons(be)
    assert set(ScheduledJob.objects.values_list("name", flat=True)) == {"a", "b"}
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    sync_crons(get_absurd_backends()["default"])
    assert set(ScheduledJob.objects.values_list("name", flat=True)) == {"a"}


def test_admin_rows_untouched(settings):
    ScheduledJob.objects.create(name="a", source="admin", alias="default",
        task="tests.tasks.add", params={"args": [], "kwargs": {}}, options={},
        cron="0 2 * * *")
    settings.TASKS = tasks({})
    sync_crons(get_absurd_backends()["default"])
    assert ScheduledJob.objects.filter(source="admin", name="a").exists()
```

- [ ] **Step 2: Run — expect FAIL** (`sync_crons` missing / no rows).

- [ ] **Step 3: Implement table phase (prose).** In `sync_crons`: open a transaction on
      `backend.database`; take `pg_advisory_xact_lock(<module const>)`; for each
      declared `Schedule` compute `params={"args":…,"kwargs":…}`,
      `options=resolve_spawn_options`, `queue=effective_queue`, then
      `ScheduledJob.objects.update_or_create(source="settings", alias=backend.alias, name=…, defaults=…)`;
      delete
      `ScheduledJob.objects.filter( source="settings", alias=backend.alias).exclude(name__in=declared_names)`.
      pg_cron phase is Task 9. All ORM (parameterized) — no string SQL here.

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** —
      `feat(scheduler): sync_crons table phase (upsert + source-scoped prune)`.

---

### Task 9: `sync_crons` — pg_cron jobs (schedule phase)

**Files:**

- Modify: `django_absurd/pgcron.py`
- Test: `tests/test_pgcron_sync_jobs.py`

**Interfaces:**

- Produces: `sync_crons` also upserts a `pg_cron` job per declared entry (constant
  command), re-arms `active`, and prunes owned-but-undeclared jobs (savepoint-swallow).

- [ ] **Step 1: Failing tests** — `tests/test_pgcron_sync_jobs.py` (real pg_cron):

```python
# helper to read cron.job rows for our prefix:
def owned_jobs(cur, alias="default"):
    cur.execute("select jobname, schedule, command from cron.job "
                "where jobname like %s order by jobname",
                [f"absurd:settings:{alias}:%"])
    return cur.fetchall()

# tests (each drives sync_crons then inspects cron.job via connection.cursor):
# - creates a job with schedule + constant command "select django_absurd_run_scheduled('a')"
# - idempotent: sync twice -> same single row
# - prune: drop entry b, re-sync -> job b gone; a hand-made non-prefixed cron.job survives
# - prune-tolerance: pre-delete a job's row via cron.unschedule, then sync -> no error
# - injection: schedule args ["'; drop schema absurd cascade; --", "$$"] -> command is the
#   constant wrapper call; `select to_regnamespace('absurd')` still non-null
```

- [ ] **Step 2: Run — expect FAIL** (no `cron.job` rows created).

- [ ] **Step 3: Implement schedule phase (prose).** After the table phase, in the same
      txn: for each declared entry run the parameterized statement
      `select cron.schedule(%s, %s, format('select django_absurd_run_scheduled(%%L)', %s::text))`
      with binds `(jobname, pg_schedule, name)` — note `%%L` (psycopg scans for `%`) and
      the `::text` cast; capture `jobid`; then
      `select cron.alter_job(%s, active := true)`. Prune:
      `select jobid from cron.job where jobname like %s` (the prefix) minus declared
      jobnames; for each stale jobid do
      `savepoint sp; select cron.unschedule(jobid); release savepoint` — on
      `InternalError` (SQLSTATE `XX000`, "could not find") roll back to the savepoint
      and continue. All `pg_cron` SQL confined to this module.

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** —
      `feat(scheduler): sync_crons pg_cron phase (upsert, re-arm, savepoint-swallow prune)`.

---

### Task 10: `teardown_crons`

**Files:**

- Modify: `django_absurd/pgcron.py`
- Test: `tests/test_pgcron_teardown.py`

**Interfaces:**

- Produces: `teardown_crons(backend) -> None` — unschedule every
  `absurd:settings:<alias>:%` job (savepoint-swallow) and delete `source="settings"`
  rows. Idempotent.

- [ ] **Step 1: Failing test** — populate via `sync_crons`, then `teardown_crons`,
      assert no owned `cron.job` rows and no `source="settings"` `ScheduledJob` rows
      remain; a second `teardown_crons` call raises nothing; a `source="admin"` row
      survives.

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement (prose).** Select owned jobids by the prefix; unschedule each
      in a savepoint swallowing not-found; delete
      `ScheduledJob.objects.filter(source="settings", alias=backend.alias)`. Same
      advisory lock.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(scheduler): teardown_crons`.

---

### Task 11: `absurd_sync_crons` management command

**Files:**

- Create: `django_absurd/management/commands/absurd_sync_crons.py`
- Test: `tests/test_absurd_sync_crons_command.py`

**Interfaces:**

- Produces: `manage.py absurd_sync_crons [--alias] [--teardown]` — runs `sync_crons` (or
  `teardown_crons` with `--teardown`); reports created/pruned counts; raises
  `CommandError` unless `SCHEDULER="pg_cron"` (except `--teardown`).

- [ ] **Step 1: Failing tests** (real pg_cron): command creates jobs + reports; refuses
      under `SCHEDULER="beat"` (assert `CommandError`); `--teardown` removes owned jobs
      even under beat.

- [ ] **Step 2: Run — expect FAIL** (command missing).
- [ ] **Step 3: Implement (prose).** Subclass `BaseCommand` (or `AbsurdReportCommand`);
      `--alias` via `resolve_backend`; `--teardown` flag. If not `--teardown` and
      `backend.scheduler != "pg_cron"` → `CommandError`. Call
      `sync_crons`/`teardown_crons`; write a short created/pruned summary.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(scheduler): absurd_sync_crons command`.

---

### Task 12: `post_migrate` reconcile

**Files:**

- Modify: `django_absurd/apps.py`
- Test: `tests/test_pgcron_post_migrate.py`

**Interfaces:**

- Produces: a `post_migrate` receiver connected **after**
  `provision_queues_after_migrate` that runs `sync_crons` when `SCHEDULER="pg_cron"`
  else `teardown_crons`; best-effort (catches
  `ImproperlyConfigured/OperationalError/ProgrammingError/InternalError/ImportError/TypeError`,
  logs + skips).

- [ ] **Step 1: Failing tests** (real pg_cron;
      `@pytest.mark.django_db(transaction=True)`): under `SCHEDULER="pg_cron"`,
      `call_command("migrate", ...)` (or invoke the receiver) creates owned `cron.job`
      rows; switching to `SCHEDULER="beat"` and re-running removes them (teardown); the
      **no-op invariant** — a job committed with a missing row fires clean (assert
      `cron.job_run_details` has no error status for it); a schedule with a bad dotted
      path does not crash migrate (best-effort skip).

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement (prose).** In `ready()` `post_migrate.connect(...)` a new
      receiver **after** the queue-provision connect. Receiver: for each backend, if
      `scheduler == "pg_cron"` call `sync_crons` else `teardown_crons`, wrapped in the
      best-effort try/except (log + continue). Import inside the function (match the
      existing receiver's local-import style).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(scheduler): reconcile pg_cron crons on post_migrate`.

---

### Task 13: end-to-end + docs + example

**Files:**

- Test: `tests/test_pgcron_e2e.py`
- Modify: `docs/web/cron-jobs.md`, `django_absurd/AGENTS.md`, `examples/app.py`,
  `examples/compose.yaml`, `examples/README.md`, a new example migration, `docs/WHY.md`

**Interfaces:** none (integration + docs).

- [ ] **Step 1: e2e failing test** (real pg_cron): declare
      `SCHEDULE {"e": {"task": "tests.tasks.create_payload", "cron": "* * * * *", "args": ["e2e"]}}`
      under `SCHEDULER="pg_cron"`; `sync_crons`; trigger the job
      (`select django_absurd_run_scheduled('e')` to avoid waiting a minute, OR advance
      and let pg_cron fire); `absurd_worker --burst`; assert `Payload` row. (Firing via
      the wrapper directly is deterministic; keep a separate slow/marked test for real
      pg_cron timing if desired.)

- [ ] **Step 2: Run — expect FAIL / then PASS after wiring** (this exercises the full
      path; if Tasks 1–12 pass it should pass — treat a failure as an integration gap to
      fix).

- [ ] **Step 3: Docs (prose).** `docs/web/cron-jobs.md` Database-side: "coming soon" →
      real (enable extension ≥1.4, `SCHEDULER="pg_cron"`, `absurd_sync_crons` + auto on
      migrate, TZ note both framings, sub-minute=beat-only, mutual exclusion,
      single-role, the two loud callouts: kill-via-SCHEDULE-not-`cron.alter_job`,
      uninstall-not-self-cleaning
  - `cron.job_run_details` purge). `AGENTS.md` scheduling section: pg_cron backend,
    selector, reconcile, wrapper model, install prereq. README unchanged.

- [ ] **Step 4: Example (prose).** `examples/`: switch the db service to the
      `pg_cron`-enabled image + `shared_preload_libraries`/`cron.database_name`; add a
      migration **in the example app** doing `CreateExtension("pg_cron")` (demonstrates
      the user-owned pattern); set `OPTIONS["SCHEDULER"]="pg_cron"` + a `SCHEDULE` ping;
      run `docker compose up --build --abort-on-container-exit` and confirm the task
      fires.

- [ ] **Step 5: WHY.md (prose).** Run `capture-why` to record the projection-table /
      constant-command / settings-vs-admin rationale.

- [ ] **Step 6: Commit** — `feat(scheduler): pg_cron e2e, docs, example`.

---

## Self-review

- **Spec coverage:** selector (T4), model (T2), wrapper+search-path (T3), reconcile
  table+jobs (T8/T9), teardown (T10), command (T11), post_migrate+deploy (T12), E007
  static incl. 6-field/name/jobname/queue/SCHEDULER (T7), spawn parity incl.
  default_max_attempts + SDK normalize (T5), effective queue (T5), injection (T9), no-op
  invariant (T12), install (T1 infra + T13 example, no shipped CREATE EXTENSION), docs
  (T13). ✔ all mapped.
- **Coverage gaps to watch during execution:** the `cron.alter_job` re-arm has no
  dedicated test — add an assertion in a T9 test (operator disables a job, sync re-arms
  `active=true`). Add it when implementing T9.
- **Type consistency:** `sync_crons(backend)`, `teardown_crons(backend)`,
  `resolve_spawn_options(backend, schedule) -> dict`,
  `effective_queue(schedule) -> str`,
  `build_jobname(alias, name, source="settings") -> str`, `backend.scheduler` — used
  consistently across T5–T12.
- **No pre-written impl:** impl steps are prose; only tests carry code. ✔
