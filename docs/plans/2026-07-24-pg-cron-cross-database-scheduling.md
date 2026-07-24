# Cross-Database pg_cron Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `django_absurd.pg_cron` schedule jobs from a central metadata DB into the
app DB via `cron.schedule_in_database`, so the app/test DB never holds the pg_cron
extension — restoring pytest-xdist + `test_` isolation.

**Architecture:** `cron.database_name` becomes a central metadata DB (≠ app DB) holding
the extension + `cron.job`. All eleven `cron.*` sites route through one new seam
(`pg_cron/catalog.py`) that opens a raw short-lived psycopg connection to the central DB
(dbname-swapped from the app connection's params) and schedules jobs
`database => <app db>`. Jobs are db-namespaced (`_dj:<target_db>:<source>:<name>`) to
defeat upsert-steal, and bound to their target DB so a prod schedule can never fire into
a test DB. A detection leaf makes the seam inert under tests by default.

**Tech Stack:** Django 6.0, Python 3.12, psycopg v3, pg_cron ≥ 1.4 (live-verified 1.6),
pytest (function-based), pytest-django, pytest-xdist.

**Spec:** `docs/specs/2026-07-23-pg-cron-test-inert-design.md` — read it first; this
plan implements its §Suggested task order.

## Global Constraints

- **Runtime floor:** Django 6.0 / Python 3.12; psycopg (v3) Django backend mandatory
  (SDK reuses the connection).
- **pg_cron floor:** ≥ 1.4 (gate on `to_regproc('cron.schedule_in_database')`, never
  version parsing).
- **App/test DB never holds the extension and never touches `cron.*`.** Only the central
  DB (`cron.database_name`) does.
- **Jobname scheme:** `_dj:<target_db>:<source>:<name>`. Match with `starts_with(...)`,
  NEVER `LIKE` (`_` is a LIKE wildcard present in every `test_x_gwN` name).
- **Detection:** a `cron.*` op is inert when
  `test_environment_active() OR is_test_database(alias)`, UNLESS opt-in
  `OPTIONS["PG_CRON_ON_TEST_DB"]` is True. EITHER predicate (not AND) → prod-safe.
- **Central connection:** raw psycopg via `connections[alias].get_connection_params()`
  with `dbname` swapped; `autocommit=True`; every op wrapped in
  `connections[alias].wrap_database_errors` so `psycopg.*` surfaces as
  `django.db.utils.*` with `__cause__` set (B1).
- **Scoped teardown/flush:** `WHERE database = <LIVE db name>` (`current_database()` /
  live `settings_dict["NAME"]`), NEVER `ORIGINAL_DATABASE_NAMES`.
- **Testing:** pytest function-based ONLY (never class-based). No `unittest.mock` /
  monkeypatch — drive real DB conditions. HTTP mocking (if ever) via `responses`. Assert
  the COMPLETE check/error message text, never a fragment. Alphabetize
  `@pytest.mark.parametrize` values, fixture `params`, AND a test's own fixture
  parameters. Test through real entrypoints (commands, checks, save/delete signals,
  admin HTTP), not helper units.
- **Style:** `import typing as t` (never `from typing import X`); absolute imports only;
  functions contain a verb; no leading-underscore module constants/helpers; helpers
  below their public callers; test helpers in `utils.py`.
- **Two test DBs at play:** the migrate-reconcile gate (`should_sync_schedules`, keyed
  on `ORIGINAL_DATABASE_NAMES` + `SYNC_SCHEDULES_ON_TEST_DB`) and the new catalog
  inert-leaf (`PG_CRON_ON_TEST_DB`) coexist. The opt-in pg_cron suite must set BOTH
  `SYNC_SCHEDULES_ON_TEST_DB=True` AND `PG_CRON_ON_TEST_DB=True`.
- **Git:** `/usr/bin/git`; commit incrementally; new commits only (never `--amend`); no
  AI attribution in commits.

## File Structure

New modules:

- `django_absurd/pg_cron/detection.py` — leaf predicates (`is_test_database`,
  `test_environment_active`, `is_pg_cron_inert`) + the `ORIGINAL_DATABASE_NAMES`
  snapshot moved here (no heavy imports → importable by migrations/models with no
  cycle).
- `django_absurd/pg_cron/catalog.py` — the single `cron.*` seam. Owns the one
  db-namespaced `build_jobname(database, source, name="")` constructor. Opens the
  central connection, applies the inert gate, exposes verbs (`schedule_job`,
  `unschedule_jobs_for_database`, `get_job`, `get_managed_jobs`, `prune_jobs`,
  `probe_cron_grammar`, `reconcile_cleanup_job`).

Modified:

- `django_absurd/connection.py` — add `resolve_cron_database(alias)` +
  `open_central_connection(alias)` (raw psycopg + B1 wrap).
- `django_absurd/pg_cron/validators.py` — DELETE the `build_jobname` /
  `build_jobname_prefix` constructors (moved to `catalog.py`) and
  `validate_jobname_length` (unbounded `text`, no cap). Keeps only real validators.
- `django_absurd/pg_cron/models.py` — `PgCronManager` + `schedule_pg_cron_job` /
  `unschedule_pg_cron_job` route through catalog; fold `active` into
  `schedule_in_database`, drop `cron.alter_job`.
- `django_absurd/pg_cron/reconcile.py` — route through catalog; central cleanup job.
- `django_absurd/pg_cron/signals.py` — `transaction.on_commit` emission; swallow-and-log
  after commit; contract rewrite.
- `django_absurd/pg_cron/apps.py` — snapshot populates
  `detection.ORIGINAL_DATABASE_NAMES`; `should_sync_schedules` uses
  `detection.is_test_database`.
- `django_absurd/pg_cron/checks.py` — DELETE the jobname-length check + hint; add
  composition + central-extension checks.
- `django_absurd/flush.py` — scoped `drop_pg_cron_state`.
- `django_absurd/backends.py` — `CRON_DATABASE_NAME`, `PG_CRON_ON_TEST_DB` in
  `AbsurdBackendOptions`.
- `django_absurd/pytest_plugin.py` — session-scoped autouse start-sweep fixture; keep
  `absurd_drain_queue`.
- `django_absurd/pg_cron/management/commands/absurd_sync_crons.py` — `CommandError` when
  inert.
- `django_absurd/pg_cron/migrations/0001_initial.py` — drop `CreateExtension`.
- `examples/pg_cron/Dockerfile.pg_cron` + `compose.yaml`; `tests/pg_cron/settings.py` +
  `utils.py`.
- Docs: `docs/WHY.md`, `django_absurd/AGENTS.md`, `docs/web/`, `CLAUDE.md`,
  `.claude/skills/pg-cron/SKILL.md`.

**Note on a spec/reality discrepancy:** the spec's §Homes says the open-helper sits "in
`connection.py` alongside `resolve_absurd_database`," but `resolve_absurd_database`
actually lives in `django_absurd/queues.py`. This plan puts the new helper in
`connection.py` (the natural home for connection helpers) and imports
`resolve_absurd_database` from `queues`.

---

### Task 1: Detection leaf (`pg_cron/detection.py`)

**Files:**

- Create: `django_absurd/pg_cron/detection.py`
- Modify: `django_absurd/pg_cron/apps.py:26` (move `ORIGINAL_DATABASE_NAMES`),
  `apps.py:34-37` (populate the leaf's dict in `ready()`), `apps.py:127-133`
  (`should_sync_schedules` calls `detection.is_test_database`)
- Test: `tests/pg_cron/test_detection.py`

**Interfaces:**

- Consumes: `django.db.connections`, `django.conf.settings`,
  `django.test.utils._TestState`, `django_absurd.backends.get_absurd_backends`.
- Produces:
  - `ORIGINAL_DATABASE_NAMES: dict[str, str]` (module-level, populated by
    `apps.ready()`)
  - `is_test_database(alias: str) -> bool` — live
    `connections[alias].settings_dict["NAME"]` differs from the
    `ORIGINAL_DATABASE_NAMES` snapshot.
  - `test_environment_active() -> bool` — `hasattr(_TestState, "saved_data")` (set by
    `setup_test_environment`, removed by teardown). A module-load guard fails loudly if
    `_TestState` is gone.
  - `is_pg_cron_inert(alias: str) -> bool` —
    `(test_environment_active() or is_test_database(alias)) and not pg_cron_on_test_db(alias)`,
    where `pg_cron_on_test_db` reads `OPTIONS["PG_CRON_ON_TEST_DB"]` (default False) off
    the alias's backend.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pg_cron/test_detection.py
import pytest
from django.db import connections

from django_absurd.pg_cron import detection
from tests.pg_cron import utils


def test_test_environment_active_true_under_pytest() -> None:
    # setup_test_environment ran → the signal is present for the whole suite.
    assert detection.test_environment_active() is True


def test_is_test_database_true_when_live_name_differs_from_snapshot(
    settings: object,
) -> None:
    alias = "default"
    detection.ORIGINAL_DATABASE_NAMES[alias] = "some_prod_name"
    assert connections[alias].settings_dict["NAME"] != "some_prod_name"
    assert detection.is_test_database(alias) is True


def test_is_test_database_false_when_live_name_matches_snapshot() -> None:
    alias = "default"
    live_name = str(connections[alias].settings_dict["NAME"])
    detection.ORIGINAL_DATABASE_NAMES[alias] = live_name
    assert detection.is_test_database(alias) is False


def test_is_pg_cron_inert_true_under_tests_without_opt_in(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    assert detection.is_pg_cron_inert("default") is True


def test_is_pg_cron_inert_false_when_opt_in(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    assert detection.is_pg_cron_inert("default") is False
```

Extend `tests/pg_cron/utils.py`'s `build_pg_cron_tasks` with a
`pg_cron_on_test_db: bool = False` keyword that sets `OPTIONS["PG_CRON_ON_TEST_DB"]`
(alphabetized keyword).

- [ ] **Step 2: Run tests to verify they fail**

Run:
`docker compose up -d db db_pg_cron && uv run pytest tests/pg_cron/test_detection.py -v`
Expected: FAIL — `ModuleNotFoundError: django_absurd.pg_cron.detection` (and
`build_pg_cron_tasks() got an unexpected keyword argument`).

- [ ] **Step 3: Implement the leaf (prose)**

Create `detection.py` as a dependency-light leaf: module-level
`ORIGINAL_DATABASE_NAMES: dict[str, str] = {}`. At module load,
`from django.test.utils import _TestState` inside an `install_absurd_cleanup`-style
guard so a Django rename fails loudly. `test_environment_active()` returns
`hasattr(_TestState, "saved_data")`. `is_test_database(alias)` compares the live
`settings_dict["NAME"]` against the snapshot. `pg_cron_on_test_db(alias)` reads the
alias's backend `OPTIONS` (via `get_absurd_backends()`), defaulting False.
`is_pg_cron_inert(alias)` composes them per the interface. Then in `apps.py`: move the
`ORIGINAL_DATABASE_NAMES` dict reference to populate `detection.ORIGINAL_DATABASE_NAMES`
in `ready()`, and rewrite `should_sync_schedules` to call
`detection.is_test_database(backend.database)` instead of its inline comparison.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_detection.py tests/pg_cron/test_sync_schedules_on_migrate.py -v`
Expected: PASS (the migrate-gate test still green — `should_sync_schedules` behavior
unchanged).

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/detection.py django_absurd/pg_cron/apps.py tests/pg_cron/test_detection.py tests/pg_cron/utils.py
/usr/bin/git commit -m "feat(pg_cron): add detection leaf (is_test_database / test_environment_active / is_pg_cron_inert)"
```

---

### Task 2: Central connection + B1 error-wrap (`connection.py`)

**Files:**

- Modify: `django_absurd/connection.py` (add helpers), `django_absurd/backends.py:73-81`
  (declare `CRON_DATABASE_NAME` in `AbsurdBackendOptions`)
- Test: `tests/pg_cron/test_central_connection.py`

**Interfaces:**

- Consumes: `django_absurd.queues.resolve_absurd_database`, `django.db.connections`,
  `psycopg`.
- Produces:
  - `resolve_cron_database(alias: str) -> str` — `OPTIONS["CRON_DATABASE_NAME"]` if set,
    else `current_setting('cron.database_name', true)` read on the app connection (NULL
    → raise a clear `ImproperlyConfigured`).
  - `open_central_connection(alias: str)` — `@contextmanager` yielding a psycopg cursor
    on the central DB. Built from `connections[alias].get_connection_params()` (pop
    `cursor_factory`), `dbname` swapped to `resolve_cron_database(alias)`,
    `autocommit=True`. The yielded work runs inside
    `connections[alias].wrap_database_errors` so `psycopg.*` errors re-raise as
    `django.db.utils.*` with `__cause__` set. Closes the connection on exit.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pg_cron/test_central_connection.py
import psycopg
import pytest
from django.db import ProgrammingError

from django_absurd import connection
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_open_central_connection_reaches_central_db(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    with connection.open_central_connection("default") as cur:
        cur.execute("select current_database()")
        (dbname,) = cur.fetchone()
    assert dbname == connection.resolve_cron_database("default")


@pytest.mark.django_db(transaction=True)
def test_open_central_connection_translates_psycopg_errors(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    with pytest.raises(ProgrammingError) as excinfo:
        with connection.open_central_connection("default") as cur:
            cur.execute("select * from this_table_does_not_exist")
    assert isinstance(excinfo.value.__cause__, psycopg.Error)
    assert getattr(excinfo.value.__cause__, "sqlstate", None) == "42P01"
```

The `tests/pg_cron` suite runs against `db_pg_cron`, whose `cron.database_name` the
settings pin — so `resolve_cron_database` auto-discovers it. (Task 8 finalizes the
suite's DB story; for now the central DB is reachable on that service.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_central_connection.py -v` Expected: FAIL —
`AttributeError: module 'django_absurd.connection' has no attribute 'open_central_connection'`.

- [ ] **Step 3: Implement the helpers (prose)**

In `connection.py`: `resolve_cron_database(alias)` reads the backend
`OPTIONS["CRON_DATABASE_NAME"]`; if absent, opens `connections[alias].cursor()` and
`select current_setting('cron.database_name', true)`, raising `ImproperlyConfigured` on
NULL (non-pg_cron server) with a hint to set `CRON_DATABASE_NAME`.
`open_central_connection(alias)` follows the proven `worker.py:171-175` template: copy
`get_connection_params()`, `params.pop("cursor_factory", None)`, override `dbname`,
`psycopg.connect(**params, autocommit=True)`; wrap the yielded cursor usage in
`with connections[alias].wrap_database_errors:` so errors translate; close in a
`finally`. Declare `CRON_DATABASE_NAME: str` in `AbsurdBackendOptions`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pg_cron/test_central_connection.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/connection.py django_absurd/backends.py tests/pg_cron/test_central_connection.py
/usr/bin/git commit -m "feat(pg_cron): central-connection helper with psycopg->Django error translation (B1)"
```

---

### Task 3: Remove the jobname-length restriction

`cron.job.jobname` is unbounded `text` (LIVE-VALIDATED: a 300-char jobname round-trips
intact via `schedule_in_database`, no truncation). There is NO length restriction to
enforce — delete the 63-byte `validate_jobname_length` guard, its model `clean` call,
and its `checks.py` hint + branch entirely. Keep `validate_name_charset` (the
`[A-Za-z0-9_-]` charset guard stays — jobnames use `:` as a separator). This task does
NOT touch the jobname builders — those move to `catalog.py` and gain the db-namespace in
Task 4 (a name constructor doesn't belong in `validators.py`).

**Files:**

- Modify: `django_absurd/pg_cron/validators.py:46-54` (DELETE
  `validate_jobname_length`), `django_absurd/pg_cron/models.py:200-227` (drop the
  `validate_jobname_length` call in `clean`), `django_absurd/pg_cron/checks.py` (DELETE
  `E007_HINT_PG_CRON_JOBNAME` at :22-25, its import at :15, and the
  `validate_jobname_length` call + branch at :79-84)
- Delete: `tests/pg_cron/validators/test_jobname_length.py`
- Test: `tests/pg_cron/test_scheduledtask_model.py`

**Interfaces:**

- Removes: `validate_jobname_length`, `E007_HINT_PG_CRON_JOBNAME`. No new symbols.

- [ ] **Step 1: Write the failing test**

```python
# tests/pg_cron/test_scheduledtask_model.py  (add — a very long name now validates cleanly)
import pytest

from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_long_schedule_name_passes_full_clean(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    task = ScheduledTask(
        name="n" * 300, source=Source.ADMIN, task="tests.pg_cron.tasks.add",
        queue="default", cron="5 seconds",
    )
    task.full_clean()  # no ValidationError — length is unbounded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pg_cron/test_scheduledtask_model.py -v` Expected: FAIL —
`validate_jobname_length` still present and rejects the 300-char name.

- [ ] **Step 3: Implement (prose)**

DELETE `validate_jobname_length` from `validators.py`, its call in
`ScheduledTask.clean`, and — in `checks.py` — the import, the
`E007_HINT_PG_CRON_JOBNAME` constant, and the `validate_jobname_length` branch of
`check_pg_cron_name`. Leave the `build_jobname` / `build_jobname_prefix` builders
untouched (Task 4 moves + db-namespaces them).

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_scheduledtask_model.py tests/pg_cron/test_pg_cron_checks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/validators.py django_absurd/pg_cron/models.py django_absurd/pg_cron/checks.py tests/pg_cron/test_scheduledtask_model.py
/usr/bin/git rm tests/pg_cron/validators/test_jobname_length.py
/usr/bin/git commit -m "feat(pg_cron): drop the (unnecessary) jobname-length restriction"
```

---

### Task 4: Catalog seam — schedule/unschedule/read verbs (`catalog.py`) + wire models

**Files:**

- Create: `django_absurd/pg_cron/catalog.py`
- Modify: `django_absurd/pg_cron/models.py:66-127` (`PgCronManager` → catalog),
  `models.py:255-287` (`schedule_pg_cron_job` folds `active` into
  `schedule_in_database`, drops `cron.alter_job`; `unschedule_pg_cron_job` → catalog),
  `django_absurd/pg_cron/validators.py:36-43` (DELETE `build_jobname` +
  `build_jobname_prefix` — they move to `catalog.py`)
- Test: `tests/pg_cron/test_catalog.py`; move the jobname-builder tests here (out of
  `test_pg_cron_naming.py`); adjust `tests/pg_cron/test_schedule_emission.py`,
  `test_pg_cron_sync_jobs.py`

**Interfaces:**

- Consumes: `connection.open_central_connection`, `detection.is_pg_cron_inert`,
  `queues.resolve_absurd_database`. The seam resolves the LIVE app DB name once.
- Produces the single jobname builder (moved out of `validators.py`, db-namespaced):
  - `build_jobname(database: str, source: str, name: str = "") -> str` →
    `f"_dj:{database}:{source}:{name}"`; `name=""` yields the `starts_with` prefix. The
    seam is its ONLY caller; the `<db>` segment is always present. (Future, out of
    scope: a `database` field on `ScheduledTask` for real multi-DB — for now db is
    derived from the single Absurd connection.)
- Produces the verbs (all take `alias: str`, all no-op early when
  `is_pg_cron_inert(alias)`):
  - `schedule_job(alias, *, name, source, cron, command, active) -> None` —
    `select cron.schedule_in_database(%s, %s, %s, %s, NULL, %s)` with the db-namespaced
    jobname and `database => <app db name>`; `active` is the 6th argument (no
    `alter_job`).
  - `unschedule_job(alias, *, name, source) -> None`
  - `unschedule_jobs_for_database(alias, *, source=None) -> None` — scoped
    `WHERE database = <live app db> AND starts_with(jobname, prefix)`.
  - `get_job(alias, *, name, source) -> PgCronJobRow | None`
  - `get_managed_jobs(alias, *, source=None) -> list[PgCronJobRow]`
  - `prune_jobs(alias, *, source, keep_names) -> None`
- The app-DB name passed as `database =>` is the LIVE
  `connections[alias].settings_dict["NAME"]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pg_cron/test_catalog.py
import pytest
from django.db import connections

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from tests.pg_cron import utils


def test_build_jobname_includes_target_database() -> None:
    assert (
        catalog.build_jobname("app_db", Source.SETTINGS, "nightly")
        == "_dj:app_db:s:nightly"
    )


def test_build_jobname_without_name_is_the_prefix() -> None:
    assert catalog.build_jobname("test_x_gw1", Source.SETTINGS) == "_dj:test_x_gw1:s:"


@pytest.fixture
def _opt_in(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)


@pytest.mark.django_db(transaction=True)
def test_schedule_job_binds_to_app_database(_opt_in: None) -> None:
    catalog.schedule_job(
        "default",
        name="probe",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    row = catalog.get_job("default", name="probe", source=Source.SETTINGS)
    assert row is not None
    live_db = str(connections["default"].settings_dict["NAME"])
    with catalog.open_central_connection_for_test("default") as cur:  # test helper in utils
        cur.execute(
            "select database, active from cron.job where jobname = %s",
            [f"_dj:{live_db}:{Source.SETTINGS}:probe"],
        )
        database, active = cur.fetchone()
    assert database == live_db
    assert active is True


@pytest.mark.django_db(transaction=True)
def test_schedule_job_is_noop_when_inert(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    catalog.schedule_job(
        "default",
        name="probe",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    assert catalog.get_job("default", name="probe", source=Source.SETTINGS) is None
```

(Prefer reading `cron.job` via a small `utils.py` helper rather than an ad-hoc catalog
method — keep the seam's public surface to the verbs above.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_catalog.py -v` Expected: FAIL —
`ModuleNotFoundError: django_absurd.pg_cron.catalog`.

- [ ] **Step 3: Implement the seam + wire models (prose)**

Create `catalog.py`. Move the single `build_jobname(database, source, name="")` here
(deleting `build_jobname` + `build_jobname_prefix` from `validators.py`) — it's a name
constructor, not a validator, and the seam is its only caller. Each verb opens
`connection.open_central_connection(alias)`, returns early if
`detection.is_pg_cron_inert(alias)`, and runs the `cron.*` SQL against the central DB,
passing the LIVE app-DB name as the `database` argument and building db-namespaced
jobnames via `build_jobname`. `schedule_job` uses
`cron.schedule_in_database(name, schedule, command, database, NULL, active)` — folding
`active` into the 6th positional and DELETING the old `cron.alter_job(active:=…)`
follow-up (`models.py:273-275`). Move `prune_jobs` savepoint/`XX000` tolerance here (it
must run inside the B1 wrapper so `__cause__.sqlstate` survives). Then rewrite
`PgCronManager` methods and `ScheduledTask.schedule_pg_cron_job` /
`unschedule_pg_cron_job` to delegate to the catalog verbs (they keep their current
public signatures; the advisory lock moves onto the central connection — see Task 6 for
the lock's new home; here a per-op `select pg_advisory_xact_lock` on the central conn is
acceptable). `open_locked_cursor` (`models.py:290-297`) is superseded for `cron.*` —
leave it only if a non-cron caller remains, else remove.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_catalog.py tests/pg_cron/test_pg_cron_sync_jobs.py tests/pg_cron/test_schedule_emission.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/catalog.py django_absurd/pg_cron/models.py django_absurd/pg_cron/validators.py tests/pg_cron/test_catalog.py
/usr/bin/git commit -m "feat(pg_cron): catalog seam for cron.* via schedule_in_database; single db-namespaced build_jobname; drop alter_job"
```

---

### Task 5: Route reconcile + validators probe + flush through the seam

**Files:**

- Modify: `django_absurd/pg_cron/reconcile.py` (all `cron.*` via catalog; central
  cleanup job), `django_absurd/pg_cron/validators.py:78-101` (`validate_pg_cron_cron` →
  `catalog.probe_cron_grammar`), `django_absurd/flush.py:84-95` (`drop_pg_cron_state`
  scoped)
- Test: adjust `tests/pg_cron/test_cleanup_schedule.py`, `test_pg_cron_teardown.py`,
  `test_pytest_plugin.py`, `validators/test_cron.py`; new
  `tests/pg_cron/test_flush_scoped.py`

**Interfaces:**

- Adds to catalog: `probe_cron_grammar(alias, *, cron) -> None` (schedule-then-rollback
  via `schedule_in_database`, inside the granted set — NOT bare `cron.schedule`);
  `reconcile_cleanup_job(alias, *, cron, command) -> None` /
  `unschedule_cleanup_job(alias) -> None` with a db-namespaced cleanup jobname;
  `flush_database_jobs(alias) -> None` (scoped unschedule
  `WHERE database = <live> AND starts_with(jobname,'_dj:')` +
  `DELETE FROM cron.job_run_details WHERE database = <live>`).
- `reconcile.py`'s `sync_crons` / `sync_admin_crons` / `teardown_crons` call the catalog
  verbs instead of `open_locked_cursor`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pg_cron/test_flush_scoped.py
import pytest
from django.db import connections

from django_absurd.flush import flush_absurd_state
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_flush_only_removes_this_database_jobs(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    live_db = str(connections["default"].settings_dict["NAME"])
    catalog.schedule_job(
        "default", name="mine", source=Source.SETTINGS,
        cron="5 seconds", command="select 1", active=True,
    )
    utils.schedule_control_job_in_other_database("other_db_name")  # helper: raw central insert
    flush_absurd_state()
    assert catalog.get_job("default", name="mine", source=Source.SETTINGS) is None
    assert utils.control_job_still_present("other_db_name") is True
    utils.remove_control_job("other_db_name")
```

```python
# tests/pg_cron/validators/test_cron.py  (adjust: probe uses schedule_in_database, still ValidationError on bad grammar)
import pytest
from django.core.exceptions import ValidationError

from django_absurd.pg_cron.validators import validate_pg_cron_cron


@pytest.mark.django_db(transaction=True)
def test_validate_pg_cron_cron_rejects_bad_grammar() -> None:
    with pytest.raises(ValidationError):
        validate_pg_cron_cron("not a cron", "default")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`uv run pytest tests/pg_cron/test_flush_scoped.py tests/pg_cron/validators/test_cron.py -v`
Expected: FAIL — `flush` still blanket-wipes / probe helper missing.

- [ ] **Step 3: Implement (prose)**

Rewrite `drop_pg_cron_state` to call `catalog.flush_database_jobs(alias)` (scoped
unschedule + scoped `DELETE FROM cron.job_run_details`, never blanket `TRUNCATE`); keep
the `TRUNCATE django_absurd_scheduledtask CASCADE` (that's the app-DB row table,
correct). Route `reconcile.py`'s three functions through the catalog verbs; the cleanup
job becomes `catalog.reconcile_cleanup_job` with a db-namespaced name (breaking the
shared `absurd_cleanup_all` identity, per spec §Cleanup job). `validate_pg_cron_cron`
delegates to `catalog.probe_cron_grammar` (which schedules a throwaway
`_dj:__probe__:<uuid>` via `schedule_in_database` and rolls back), re-raising
`DatabaseError` as `ValidationError` as today. Add the three `utils.py` control-job
helpers.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_flush_scoped.py tests/pg_cron/test_cleanup_schedule.py tests/pg_cron/test_pg_cron_teardown.py tests/pg_cron/validators/test_cron.py tests/pg_cron/test_pytest_plugin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/reconcile.py django_absurd/pg_cron/validators.py django_absurd/flush.py tests/pg_cron/test_flush_scoped.py tests/pg_cron/utils.py
/usr/bin/git commit -m "feat(pg_cron): route reconcile/probe/flush through the catalog seam; scoped flush"
```

---

### Task 6: on_commit emission + reconcile control-flow rework (`signals.py`)

**Files:**

- Modify: `django_absurd/pg_cron/signals.py:43-59` (register `transaction.on_commit`
  callbacks; swallow-and-log after commit), `django_absurd/pg_cron/reconcile.py`
  (central advisory lock; bulk body order)
- Test: `tests/pg_cron/test_schedule_emission.py` (convert to `transaction=True` /
  `django_capture_on_commit_callbacks`)

**Interfaces:**

- Save signal → `transaction.on_commit(lambda: instance.schedule_pg_cron_job())`; delete
  signal → `transaction.on_commit(lambda: catalog.unschedule_job(...))`. Both open the
  central connection AFTER commit. Central failure after the row committed →
  `logger.warning(..., exc_info=True)`, never a 500.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pg_cron/test_schedule_emission.py  (representative — convert from post_save-fires-synchronously)
import pytest
from django.test import TestCase  # NOT used — function-based below

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_save_emits_job_only_after_commit(
    django_capture_on_commit_callbacks: object, settings: object
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    with django_capture_on_commit_callbacks(execute=True):
        ScheduledTask.objects.create(
            name="onsave", source=Source.ADMIN, task="tests.pg_cron.tasks.add",
            queue="default", cron="5 seconds",
        )
    assert catalog.get_job("default", name="onsave", source=Source.ADMIN) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_schedule_emission.py -v` Expected: FAIL —
emission no longer happens synchronously in `post_save` until `on_commit` wiring lands
(or the assertion timing is wrong under the old code).

- [ ] **Step 3: Implement (prose)**

Rewrite `signals.schedule_job_on_save` / `unschedule_job_on_delete` to register
`transaction.on_commit` callbacks that call the catalog (the callback body
swallows-and-logs a post-commit central failure). Rewrite the module docstring's
emission contract (two connections; emission after commit; the advisory lock now
serializes only the central op). In `reconcile.py`, take the central advisory lock on
the central connection and run the bulk body in order: upsert declared jobs → prune
orphaned jobs (source + `WHERE database = <live>`) → reconcile cleanup job. Note
explicitly in comments that lost row↔job atomicity is acceptable because the run-wrapper
re-reads the row each fire.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_schedule_emission.py tests/pg_cron/test_cross_source_coexistence.py tests/pg_cron/test_admin/ -v`
Expected: PASS (admin HTTP save/delete still emits via the commit hook).

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/signals.py django_absurd/pg_cron/reconcile.py tests/pg_cron/test_schedule_emission.py
/usr/bin/git commit -m "feat(pg_cron): emit schedules via transaction.on_commit; central-lock reconcile body"
```

---

### Task 7: Drop CreateExtension + central compose + move `tests/pg_cron` to an ordinary test DB

**Files:**

- Modify: `django_absurd/pg_cron/migrations/0001_initial.py:69` (remove
  `CreateExtension("pg_cron")`), `examples/pg_cron/Dockerfile.pg_cron` +
  `examples/pg_cron/compose.yaml` (central `cron.database_name`, e.g. `postgres`;
  `CREATE EXTENSION` + grants via `/docker-entrypoint-initdb.d`),
  `tests/pg_cron/settings.py` (drop the `TEST["NAME"] == cron.database_name` pin; set
  `CRON_DATABASE_NAME`; set BOTH `SYNC_SCHEDULES_ON_TEST_DB` + `PG_CRON_ON_TEST_DB`),
  `tests/pg_cron/utils.py`
- Test: full `tests/pg_cron` suite must pass on the new topology (incl. under
  `-p no:cacheprovider -n 2` xdist smoke)

- [ ] **Step 1: Write the failing test**

```python
# tests/pg_cron/test_migration_has_no_create_extension.py
from django.contrib.postgres.operations import CreateExtension
from django.db import connections
from django.db.migrations.loader import MigrationLoader


def test_initial_migration_declares_no_create_extension() -> None:
    loader = MigrationLoader(connections["default"])
    migration = loader.get_migration("django_absurd_pg_cron", "0001_initial")
    assert not any(isinstance(op, CreateExtension) for op in migration.operations)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pg_cron/test_migration_has_no_create_extension.py -v`
Expected: FAIL — `CreateExtension` still at `0001:69`.

- [ ] **Step 3: Implement (prose)**

Remove the `CreateExtension("pg_cron")` operation from `0001_initial.py` (state-neutral
removal; the `RunSQL` wrapper stays). Rework `Dockerfile.pg_cron` / `compose.yaml` so
`cron.database_name` is a central DB (e.g. `postgres`) with the extension +
`USAGE ON SCHEMA cron` + `EXECUTE ON cron.schedule_in_database` grants created once via
an init script (runs only on a fresh volume — call out that existing dev volumes need
`docker compose down -v` recreation). Change `tests/pg_cron/settings.py`:
`DATABASES["default"]["TEST"]` becomes an ordinary `test_<name>` (no pin to
`cron.database_name`), add `OPTIONS["CRON_DATABASE_NAME"]` pointing at the central DB,
and set both opt-in knobs. Update the CLAUDE.md `tests/pg_cron` guidance in Task 11.

- [ ] **Step 4: Run the full suite (incl. xdist smoke)**

```bash
docker compose -f examples/pg_cron/compose.yaml down -v && docker compose up -d db db_pg_cron
uv run pytest tests/pg_cron --create-db
uv run pytest tests/pg_cron -n 2   # xdist: workers migrate test_<db>_gwN with NO CREATE EXTENSION
```

Expected: PASS on both — no `--create-db` eviction dance, xdist green.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/migrations/0001_initial.py examples/pg_cron/ tests/pg_cron/settings.py tests/pg_cron/utils.py tests/pg_cron/test_migration_has_no_create_extension.py
/usr/bin/git commit -m "feat(pg_cron): drop app-DB CreateExtension; central pg_cron; tests/pg_cron on ordinary test DB"
```

---

### Task 8: System checks (composition + central-extension fail-safe)

**Files:**

- Modify: `django_absurd/pg_cron/checks.py`
- Test: `tests/pg_cron/test_scheduler_app_checks.py` (or new `test_central_checks.py`)

**Interfaces:**

- `check_pg_cron_test_db_composition(...)` — E when `SYNC_SCHEDULES_ON_TEST_DB=True`
  without `PG_CRON_ON_TEST_DB=True`.
- `check_pg_cron_central_extension(...)` — E, registered with `Tags.database` (runs at
  `migrate` / `check --database` only): when non-test + pg_cron scheduler configured →
  the central DB is reachable and has the extension. Replaces the removed migrate-time
  `CREATE EXTENSION` fail-fast.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pg_cron/test_central_checks.py
import pytest
from django.core.management import call_command

from tests.pg_cron import utils


def test_composition_check_rejects_sync_on_test_db_without_opt_in(
    capsys: pytest.CaptureFixture[str], settings: object
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True
    with pytest.raises(SystemExit):
        call_command("check", "django_absurd")
    out = capsys.readouterr().out
    assert (
        "SYNC_SCHEDULES_ON_TEST_DB is enabled but PG_CRON_ON_TEST_DB is not"
        in out
    )
```

(Assert the COMPLETE message/hint text in the real test — the fragment above is
illustrative.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pg_cron/test_central_checks.py -v` Expected: FAIL — no
composition check yet.

- [ ] **Step 3: Implement (prose)**

Add the two checks. Composition check reads both OPTIONS knobs and emits an
`absurd.E0xx` with a `msg` stating the problem and a `hint` stating the fix (never
duplicate fix text in `msg`). Central-extension check registers under `Tags.database`,
opens the central connection when non-test, and verifies
`to_regproc('cron.schedule_in_database')` (function-existence gate, not version parse);
emit E on absence/unreachable.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_central_checks.py tests/pg_cron/test_scheduler_app_checks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/checks.py tests/pg_cron/test_central_checks.py
/usr/bin/git commit -m "feat(pg_cron): composition + Tags.database central-extension system checks"
```

---

### Task 9: Command inert guard + sweeps + isolation regression tests

**Files:**

- Modify: `django_absurd/pg_cron/management/commands/absurd_sync_crons.py`
  (`CommandError` when inert), `django_absurd/pytest_plugin.py` (session-scoped autouse
  start-sweep fixture)
- Test: `tests/pg_cron/test_absurd_sync_crons_command.py` (inert `CommandError`),
  `tests/pg_cron/test_start_sweep.py`, `tests/pg_cron/test_isolation_regression.py`

**Interfaces:**

- `absurd_sync_crons` (both `sync` and `--teardown`) → `CommandError` when
  `is_pg_cron_inert(alias)`.
- Session-scoped autouse fixture in `pytest_plugin.py`: depends on `django_db_setup`,
  enters `django_db_blocker.unblock()`, guards `apps.is_installed(PG_CRON_APP_NAME)`
  BEFORE importing `catalog`, then `catalog.flush_database_jobs(alias)` once per worker
  to clear crash-orphaned jobs on the reused test-DB name.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pg_cron/test_absurd_sync_crons_command.py  (add)
import pytest
from django.core.management import CommandError, call_command

from tests.pg_cron import utils


def test_command_errors_when_inert(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    with pytest.raises(CommandError):
        call_command("absurd_sync_crons")
```

```python
# tests/pg_cron/test_isolation_regression.py
import pytest
from django.db import connections

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_sweep_removes_this_db_job_and_spares_control_job(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    live_db = str(connections["default"].settings_dict["NAME"])
    catalog.schedule_job(
        "default", name="mine", source=Source.SETTINGS,
        cron="5 seconds", command="select 1", active=True,
    )
    utils.schedule_control_job_in_other_database("unrelated_db")
    catalog.flush_database_jobs("default")
    assert catalog.get_job("default", name="mine", source=Source.SETTINGS) is None
    assert utils.control_job_still_present("unrelated_db") is True
    utils.remove_control_job("unrelated_db")


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
def test_main_db_schedule_never_fires_into_test_db(settings: object) -> None:
    # Timing-based, serial-only: schedule a task-producing cron into a NON-test DB and
    # assert the migrated test queue stays 0. Marked slow so the default suite deselects.
    ...  # implement per spec §Isolation regression tests (fixture scratch DB + sleep window)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_isolation_regression.py -v`
Expected: FAIL — command doesn't raise; sweep helpers missing.

- [ ] **Step 3: Implement (prose)**

Guard `absurd_sync_crons.handle` with `is_pg_cron_inert` → `CommandError`. Add the
session-scoped autouse start-sweep fixture to `pytest_plugin.py` per the interface
(import-safe: only import `catalog` inside the fixture body, behind the `is_installed`
guard). Implement the fast deterministic isolation test fully; scaffold the slow timing
companion and mark it `slow` (register the `slow` marker in `pytest.toml` `markers` if
absent) + serial.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_isolation_regression.py tests/pg_cron/test_start_sweep.py -v`
Expected: PASS (the slow test runs on demand: `uv run pytest tests/pg_cron -m slow`).

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/management/commands/absurd_sync_crons.py django_absurd/pytest_plugin.py tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_isolation_regression.py tests/pg_cron/test_start_sweep.py tests/pg_cron/pytest.toml
/usr/bin/git commit -m "feat(pg_cron): inert command guard, session start-sweep fixture, isolation regression tests"
```

---

### Task 10: Docs

**Files:**

- Modify: `django_absurd/AGENTS.md` (operator setup: central DB + grants + one
  scheduling role), `docs/web/cron-jobs.md` (mirror), `CLAUDE.md` (retire the
  `--create-db` eviction dance + "test DB must equal cron.database_name" doctrine;
  update the `tests/pg_cron` service description), `.claude/skills/pg-cron/SKILL.md`
  ("In this repo" section), `docs/WHY.md` (via `capture-why` at ship — reverse the
  "Extension in the app migration" rationale)

- [ ] **Step 1: Update the operator-facing docs (prose)**

AGENTS.md + `docs/web/cron-jobs.md`: document that the extension is one-time operator
setup on `cron.database_name` (central DB), not a migration; jobs run cross-DB via
`schedule_in_database`; one scheduling role for the Absurd DB; `CRON_DATABASE_NAME`
option (optional — auto-discovered when the app DB is itself `cron.database_name`, i.e.
same-DB backward compat). Build the site: `uvx zensical build` (expect "No issues
found").

- [ ] **Step 2: Update contributor docs**

CLAUDE.md: remove the `--create-db` eviction procedure and the pinned-`TEST NAME`
requirement; describe `tests/pg_cron` running on an ordinary test DB against a central
`db_pg_cron`. Update `.claude/skills/pg-cron` "In this repo".

- [ ] **Step 3: Capture the why**

Run the `capture-why` skill to reverse the WHY.md extension section (do NOT run
`archive-specs`).

- [ ] **Step 4: Cross-check + commit**

Verify command/flag/message/default copy matches code across README/AGENTS/site.

```bash
/usr/bin/git add django_absurd/AGENTS.md docs/web/ CLAUDE.md .claude/skills/pg-cron/SKILL.md docs/WHY.md
/usr/bin/git commit -m "docs: cross-database pg_cron scheduling (central DB, no app-DB extension)"
```

---

## Self-Review

**Spec coverage:** central metadata DB (T7) · central connection + B1 (T2) · one seam
(T4/T5) · jobname namespacing + drop length restriction (T3) · fold `active` / drop
`alter_job` (T4) · on_commit emission + reconcile rework (T6) · cleanup job central (T5)
· cleanup lifecycle teardown + session-start sweep (T5 teardown / T9 start) · scoped
flush (T5) · backward compat degenerate case (T2 auto-discovery) · detection leaf +
inert gate + opt-in (T1) · validate skip when inert (T4/T5 gate) · command CommandError
(T9) · composition + `Tags.database` checks (T8) · two isolation regression tests (T9) ·
migration drop + compose + suite move (T7) · WHY/docs (T10). All spec sections mapped.

**Descoped (alpha, per review):** the spec's transition sweep (B2) and jobname-length
cap are cut — this is alpha, so there are no legacy-scheme jobs to migrate (from-scratch
assumption), and `cron.job.jobname` is unbounded `text` (live-validated) so no length
guard is warranted.

**Placeholder scan:** one `...` body remains by design — T9's slow timing companion,
whose body is specified by reference to spec §Isolation regression tests (fixture
scratch DB + sleep window; the fast deterministic sweep test beside it is the always-on
guard). Every other step carries real test code or concrete prose.

**Type consistency:** the single `build_jobname(database, source, name="")` is defined
in `catalog.py` (T4) and called only there; `validators.py` no longer owns it. Catalog
verbs all take `alias: str` first + keyword-only params, consistent across T4/T5/T9.
`is_pg_cron_inert(alias)` consumed identically in T4/T5/T8/T9.
`open_central_connection(alias)` / `resolve_cron_database(alias)` consistent T2→T4.

## Execution Handoff

Plan saved to `docs/plans/2026-07-24-pg-cron-cross-database-scheduling.md`. Two
execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between
   tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session with checkpoints.

Which approach?
