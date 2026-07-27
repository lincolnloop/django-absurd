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
  `unschedule_job`, `unschedule_jobs_for_database`, `prune_jobs`, `probe_cron_grammar`,
  `flush_database_jobs`). Reads (`cron.job` inspection) happen in tests via a `utils.py`
  helper, not shipped verbs; the cleanup job uses the generic `schedule_job` /
  `unschedule_job` (no dedicated cleanup verbs); no advisory lock.

Modified:

- `django_absurd/connection.py` — add `resolve_cron_database(alias)` +
  `open_central_connection(alias)` (raw psycopg + B1 wrap).
- `django_absurd/pg_cron/validators.py` — DELETE the `build_jobname` /
  `build_jobname_prefix` constructors (moved to `catalog.py`) and
  `validate_jobname_length` (unbounded `text`, no cap). Keeps only real validators.
- `django_absurd/pg_cron/models.py` — Task 4: DELETE `get_pg_cron_job`, `PgCronJobRow`,
  and the manager READ methods `get_job`/`get_managed_jobs` (test-only); rewire
  `schedule_pg_cron_job` / `unschedule_pg_cron_job` to catalog verbs (fold `active` into
  `schedule_in_database`, drop `cron.alter_job`). Task 5 (after `reconcile.py` stops
  using them): DELETE `PgCronManager` + `open_locked_cursor`. Ordering matters —
  `reconcile.py` imports `open_locked_cursor` + calls the manager's write methods until
  Task 5, so those two deletions MUST wait for Task 5 or the app fails to import.
- `django_absurd/pg_cron/reconcile.py` — route through catalog; central cleanup job.
- `django_absurd/pg_cron/signals.py` — `transaction.on_commit` emission; swallow-and-log
  after commit; contract rewrite.
- `django_absurd/pg_cron/apps.py` — snapshot populates
  `detection.ORIGINAL_DATABASE_NAMES`; `should_sync_schedules` uses
  `detection.is_test_database`.
- `django_absurd/pg_cron/checks.py` — DELETE the jobname-length check + hint; add
  composition + central-extension checks.
- `django_absurd/flush.py` — scoped `drop_pg_cron_state`.
- `django_absurd/backends.py` — `PG_CRON_ON_TEST_DB` in `AbsurdBackendOptions`.
- `django_absurd/pytest_plugin.py` — session-scoped autouse start-sweep fixture; keep
  `absurd_drain_queue`.
- `django_absurd/pg_cron/management/commands/absurd_sync_crons.py` — `CommandError` when
  inert.
- `django_absurd/pg_cron/migrations/0001_initial.py` — drop `CreateExtension`.
- ROOT `Dockerfile.pg_cron` + ROOT `compose.yaml` — the `db_pg_cron` service the
  `tests/pg_cron` suite uses (`compose.yaml:16-27`; move `cron.database_name` OFF the
  test DB to a central DB, add the extension+grants init script).
  `tests/pg_cron/settings.py` + `utils.py`. Separately,
  `examples/pg_cron/compose.yaml` + `examples/pg_cron/Dockerfile` (the demo app — same
  central move).
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
  (`should_sync_schedules` calls `detection.is_test_database`),
  `django_absurd/backends.py:73-81` (declare `PG_CRON_ON_TEST_DB: bool` in
  `AbsurdBackendOptions`)
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
import typing as t

import pytest
from django.db import connections

from django_absurd.pg_cron import detection
from tests.pg_cron import utils


@pytest.fixture
def _restore_original_names() -> t.Iterator[None]:
    # tests mutate the module-level snapshot; restore it so the migrate gate (which keys
    # on it) isn't corrupted for the rest of the session.
    saved = dict(detection.ORIGINAL_DATABASE_NAMES)
    try:
        yield
    finally:
        detection.ORIGINAL_DATABASE_NAMES.clear()
        detection.ORIGINAL_DATABASE_NAMES.update(saved)


def test_test_environment_active_true_under_pytest() -> None:
    # setup_test_environment ran → the signal is present for the whole suite.
    assert detection.test_environment_active() is True


def test_is_test_database_true_when_live_name_differs_from_snapshot(
    _restore_original_names: None, settings: object
) -> None:
    alias = "default"
    detection.ORIGINAL_DATABASE_NAMES[alias] = "some_prod_name"
    assert connections[alias].settings_dict["NAME"] != "some_prod_name"
    assert detection.is_test_database(alias) is True


def test_is_test_database_false_when_live_name_matches_snapshot(
    _restore_original_names: None,
) -> None:
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
`pg_cron_on_test_db: bool = True` keyword that sets `OPTIONS["PG_CRON_ON_TEST_DB"]`
(alphabetized keyword). **Default is `True`** — the `tests/pg_cron` suite IS the opt-in
suite (it already forces `SYNC_SCHEDULES_ON_TEST_DB=True` at `utils.py:13`), so every
cron-writing test gets the live seam without change; only tests that deliberately assert
the INERT path (this detection test, the Task 4 no-op test, the Task 9 command-inert
test) pass `pg_cron_on_test_db=False` explicitly. This is what keeps the whole suite
green from Task 4 onward (once the catalog seam gates on the flag).

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
`is_pg_cron_inert(alias)` composes them per the interface. Declare
`PG_CRON_ON_TEST_DB: bool` in `backends.py`'s `AbsurdBackendOptions` TypedDict. Then in
`apps.py`: move the `ORIGINAL_DATABASE_NAMES` dict reference to populate
`detection.ORIGINAL_DATABASE_NAMES` in `ready()`, and rewrite `should_sync_schedules` to
call `detection.is_test_database(backend.database)` instead of its inline comparison.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_detection.py tests/pg_cron/test_sync_schedules_on_migrate.py -v`
Expected: PASS (the migrate-gate test still green — `should_sync_schedules` behavior
unchanged).

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/detection.py django_absurd/pg_cron/apps.py django_absurd/backends.py tests/pg_cron/test_detection.py tests/pg_cron/utils.py
/usr/bin/git commit -m "feat(pg_cron): add detection leaf (is_test_database / test_environment_active / is_pg_cron_inert)"
```

---

### Task 2: Central connection + B1 error-wrap (`connection.py`)

**Files:**

- Modify: `django_absurd/connection.py` (add helpers)
- Test: `tests/pg_cron/test_central_connection.py` (happy path + B1), AND
  `tests/core/test_central_connection.py` (the NULL branch — `tests/core` runs on the
  plain `db` service with no pg_cron, so `current_setting('cron.database_name', true)`
  is NULL there → covers the `ImproperlyConfigured` path the full-patch-coverage rule
  needs)

**Interfaces:**

- Consumes: `django_absurd.queues.resolve_absurd_database`, `django.db.connections`,
  `psycopg`.
- Produces:
  - `resolve_cron_database(alias: str) -> str` —
    `current_setting('cron.database_name', true)` read on the app connection; NULL
    (non-pg_cron server) → raise a clear `ImproperlyConfigured`. No override option:
    pg_cron is server-local, so the auto-discovered value is definitionally the only
    correct one.
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
    settings.TASKS = utils.build_pg_cron_tasks({})
    with connection.open_central_connection("default") as cur:
        cur.execute("select current_database()")
        (dbname,) = cur.fetchone()
    assert dbname == connection.resolve_cron_database("default")


@pytest.mark.django_db(transaction=True)
def test_open_central_connection_translates_psycopg_errors(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({})
    with pytest.raises(ProgrammingError) as excinfo:
        with connection.open_central_connection("default") as cur:
            cur.execute("select * from this_table_does_not_exist")
    assert isinstance(excinfo.value.__cause__, psycopg.Error)
    assert getattr(excinfo.value.__cause__, "sqlstate", None) == "42P01"
```

```python
# tests/core/test_central_connection.py  (plain `db`, no pg_cron on the server)
import pytest
from django.core.exceptions import ImproperlyConfigured

from django_absurd import connection


@pytest.mark.django_db
def test_resolve_cron_database_raises_when_no_pg_cron() -> None:
    with pytest.raises(ImproperlyConfigured) as excinfo:
        connection.resolve_cron_database("default")
    assert str(excinfo.value) == (
        "cron.database_name is not set — this PostgreSQL server has no pg_cron"
        " (add pg_cron to shared_preload_libraries and set cron.database_name)."
    )
```

The `tests/pg_cron` suite runs against `db_pg_cron`, whose `cron.database_name` the
settings pin — so `resolve_cron_database` auto-discovers it. (Task 7 finalizes the
suite's DB story; for now the central DB is reachable on that service.)

- [ ] **Step 2: Run tests to verify they fail**

Run:
`uv run pytest tests/pg_cron/test_central_connection.py -v && uv run pytest tests/core/test_central_connection.py -v`
Expected: FAIL —
`AttributeError: module 'django_absurd.connection' has no attribute 'open_central_connection'`
(and no `resolve_cron_database`).

- [ ] **Step 3: Implement the helpers (prose)**

In `connection.py`: `resolve_cron_database(alias)` opens `connections[alias].cursor()`
and `select current_setting('cron.database_name', true)`; on NULL raise
`ImproperlyConfigured` with the EXACT message asserted in the core test above (server
has no pg_cron; add it to `shared_preload_libraries` and set `cron.database_name`) — no
override option. `open_central_connection(alias)` follows the proven `worker.py:171-175`
template: copy `get_connection_params()`, `params.pop("cursor_factory", None)`, override
`dbname`, `psycopg.connect(**params, autocommit=True)`; wrap the yielded cursor usage in
`with connections[alias].wrap_database_errors:` so errors translate; close in a
`finally`.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_central_connection.py -v && uv run pytest tests/core/test_central_connection.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/connection.py tests/pg_cron/test_central_connection.py tests/core/test_central_connection.py
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

### Task 4: Catalog seam (`catalog.py`) + wire models

**Files:**

- Create: `django_absurd/pg_cron/catalog.py`
- Modify: `django_absurd/pg_cron/models.py` — DELETE `get_pg_cron_job` (:249-253),
  `PgCronJobRow` (:33), and the manager READ methods `get_job`/`get_managed_jobs`
  (:73-96); rewire `schedule_pg_cron_job` / `unschedule_pg_cron_job` (:255-287) to
  catalog verbs (fold `active` into `schedule_in_database`, drop `cron.alter_job`).
  **KEEP `PgCronManager` (its write methods) + `open_locked_cursor` (:290-297) —
  `reconcile.py` still imports/uses them until Task 5.**
  `django_absurd/pg_cron/validators.py:36-43` (DELETE `build_jobname` +
  `build_jobname_prefix` — they move to `catalog.py`)
- Test: `tests/pg_cron/test_catalog.py`; move the jobname-builder tests here (out of
  `test_pg_cron_naming.py`). **Convert EVERY consumer of the deleted read surface across
  the suite** — grep `ScheduledTask.pg_cron.get_job`/`get_managed_jobs` /
  `.get_pg_cron_job()` (in `test_schedule_emission.py`, `test_pg_cron_sync_jobs.py`,
  `test_absurd_sync_crons_command.py`, `test_admin/test_scheduledtask.py`,
  `test_pg_cron_post_migrate.py`, `test_sync_schedules_on_migrate.py`,
  `test_cross_source_coexistence.py`) → `utils.fetch_cron_job` with **db-namespaced**
  jobnames (old literal asserts like `"_dj:s:nightly"` become
  `catalog.build_jobname(live_db, Source.SETTINGS, "nightly")`). Each converted test
  that writes a cron must run with the opt-in on (see the opt-in note below).

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
  - `unschedule_jobs_for_database(alias, *, source) -> None` — scoped
    `WHERE database = <live app db> AND starts_with(jobname, build_jobname(<live>, source))`.
    `source` is REQUIRED; the whole-DB sweep (all sources) is `flush_database_jobs`
    (Task 5).
  - `prune_jobs(alias, *, source, keep_names) -> None`
- **No read verbs ship.** `get_job`/`get_managed_jobs` had zero production consumers
  (test-only). Tests inspect `cron.job` via a `utils.py` central-read helper instead.
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
    live_db = str(connections["default"].settings_dict["NAME"])
    catalog.schedule_job(
        "default",
        name="probe",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    database, active = utils.fetch_cron_job(f"_dj:{live_db}:{Source.SETTINGS}:probe")
    assert database == live_db
    assert active is True


@pytest.mark.django_db(transaction=True)
def test_schedule_job_is_noop_when_inert(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    live_db = str(connections["default"].settings_dict["NAME"])
    catalog.schedule_job(
        "default",
        name="probe",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    assert utils.fetch_cron_job(f"_dj:{live_db}:{Source.SETTINGS}:probe") is None
```

Add `utils.fetch_cron_job(jobname) -> tuple[str, bool] | None` — opens the central
connection and returns `(database, active)` for a jobname, else `None`. This is the
single test-side `cron.job` reader (replaces the deleted `get_job`/`get_managed_jobs`
verbs); reuse it across the suite.

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
must run inside the B1 wrapper so `__cause__.sqlstate` survives). DELETE the test-only
READ surface — `get_pg_cron_job`, `PgCronJobRow`, and the manager's
`get_job`/`get_managed_jobs` (tests use `utils.fetch_cron_job`). COPY the manager's
WRITE methods into catalog verbs (`unschedule_matching` →
`unschedule_jobs_for_database`, `prune_jobs_without_rows` → `prune_jobs`). Rewire
`ScheduledTask.schedule_pg_cron_job` / `unschedule_pg_cron_job` (called by the
save/delete signals) to delegate to `catalog.schedule_job` / `catalog.unschedule_job`.
**KEEP `PgCronManager` (its write methods) + `open_locked_cursor` until Task 5** —
`reconcile.py` still imports `open_locked_cursor` and calls the manager's write methods,
so deleting them now breaks the module import (they're removed in Task 5 once reconcile
is rewired). The catalog introduces NO advisory lock (emission is post-commit; races are
idempotent upserts / `update_or_create` / self-heal at the next reconcile).

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_catalog.py tests/pg_cron/test_pg_cron_sync_jobs.py tests/pg_cron/test_schedule_emission.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/catalog.py django_absurd/pg_cron/models.py django_absurd/pg_cron/validators.py tests/pg_cron/
/usr/bin/git commit -m "feat(pg_cron): catalog seam for cron.* via schedule_in_database; single db-namespaced build_jobname; drop alter_job"
```

---

### Task 5: Route reconcile + validators probe + flush through the seam

**Files:**

- Modify: `django_absurd/pg_cron/reconcile.py` (all `cron.*` via catalog; central
  cleanup job), `django_absurd/pg_cron/validators.py:78-101` (`validate_pg_cron_cron` →
  `catalog.probe_cron_grammar`), `django_absurd/flush.py:84-95` (`drop_pg_cron_state`
  scoped), `django_absurd/pg_cron/models.py` — **now that `reconcile.py` no longer uses
  them, DELETE `PgCronManager` + `open_locked_cursor`** (deferred from Task 4 to avoid
  an import break)
- Test: adjust `tests/pg_cron/test_cleanup_schedule.py`, `test_pg_cron_teardown.py`,
  `test_pytest_plugin.py`, the parametrized grammar tests in `validators/test_cron.py`
  (keep them at the real `validate_<source>` entrypoints — see Step 1); new
  `tests/pg_cron/test_flush_scoped.py`

**Interfaces:**

- Adds to catalog: `probe_cron_grammar(alias, *, cron) -> None` (schedule-then-rollback
  via `schedule_in_database`, inside the granted set — NOT bare `cron.schedule`);
  `flush_database_jobs(alias) -> None` (scoped unschedule
  `WHERE database = <live> AND starts_with(jobname,'_dj:')` +
  `DELETE FROM cron.job_run_details WHERE database = <live>`).
- **No dedicated cleanup verbs.** The cleanup job is scheduled/removed with the generic
  `schedule_job` / `unschedule_job` on a cleanup lane — `source="c"` (a `CLEANUP_SOURCE`
  constant), jobname `_dj:<db>:c:cleanup_all`, `command=CLEANUP_COMMAND`. The
  schedule-vs-unschedule decision (is `OPTIONS["CLEANUP"]` set?) stays in
  `reconcile.py`.
- `reconcile.py`'s `sync_crons` / `sync_admin_crons` / `teardown_crons` call the catalog
  verbs (the deleted `open_locked_cursor` had no successor — no lock).

- [ ] **Step 1: Write the failing tests**

This `test_flush_scoped` is ALSO the **structural isolation regression test** (that the
sweep only ever touches THIS DB's jobs and can't reach another DB's) — driven through
the real `flush_absurd_state` entrypoint, so there is no separate helper-level
duplicate.

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
    assert utils.fetch_cron_job(catalog.build_jobname(live_db, Source.SETTINGS, "mine")) is None
    assert utils.control_job_still_present("other_db_name") is True
    utils.remove_control_job("other_db_name")
```

For the grammar validator, do NOT add a pure-function unit test — the suite already
exercises it through the real `validate_<source>` entrypoints (the parametrized
`validate_model_and_form` subjects, per the repo's validator methodology). The only
adjustment: those grammar tests must run with `PG_CRON_ON_TEST_DB=True` (which
`build_pg_cron_tasks` now defaults to) so the probe routes LIVE through
`catalog.probe_cron_grammar` instead of being skipped by the inert gate — otherwise a
bad-cron case would stop raising `ValidationError`. Assert the complete error message as
today.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_flush_scoped.py -v` Expected: FAIL — the
new-scheme db-namespaced job written by `catalog.schedule_job` isn't matched by the old
prefix-scoped teardown, and the `utils.py` control-job helpers don't exist yet.

- [ ] **Step 3: Implement (prose)**

Rewrite `drop_pg_cron_state` to call `catalog.flush_database_jobs(alias)` (scoped
unschedule + scoped `DELETE FROM cron.job_run_details`, never blanket `TRUNCATE`); keep
the `TRUNCATE django_absurd_scheduledtask CASCADE` (that's the app-DB row table,
correct). Route `reconcile.py`'s three functions through the catalog verbs and DELETE
the now-unused `PgCronManager` + `open_locked_cursor` from `models.py` (deferred from
Task 4); the cleanup job is scheduled with the generic
`catalog.schedule_job(..., source=CLEANUP_SOURCE, name="cleanup_all", command=CLEANUP_COMMAND)`
(jobname `_dj:<db>:c:cleanup_all`, breaking the shared `absurd_cleanup_all` identity,
per spec §Cleanup job) and removed with `catalog.unschedule_job(...)` — no dedicated
cleanup verbs; the present-or-not decision stays in `reconcile.py`.
`validate_pg_cron_cron` delegates to `catalog.probe_cron_grammar` (which schedules a
throwaway `_dj:__probe__:<uuid>` via `schedule_in_database` and rolls back), re-raising
`DatabaseError` as `ValidationError` as today. Add the three `utils.py` control-job
helpers.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_flush_scoped.py tests/pg_cron/test_cleanup_schedule.py tests/pg_cron/test_pg_cron_teardown.py tests/pg_cron/validators/test_cron.py tests/pg_cron/test_pytest_plugin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/reconcile.py django_absurd/pg_cron/validators.py django_absurd/pg_cron/models.py django_absurd/flush.py tests/pg_cron/
/usr/bin/git commit -m "feat(pg_cron): route reconcile/probe/flush through the catalog seam; scoped flush; drop manager+lock"
```

---

### Task 6: on_commit emission + reconcile control-flow rework (`signals.py`)

**Files:**

- Modify: `django_absurd/pg_cron/signals.py:43-59` (register `transaction.on_commit`
  callbacks; swallow-and-log after commit), `django_absurd/pg_cron/reconcile.py` (bulk
  body order — no lock)
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
from django.db import connections

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_save_emits_job_only_after_commit(
    django_capture_on_commit_callbacks: object, settings: object
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    live_db = str(connections["default"].settings_dict["NAME"])
    jobname = catalog.build_jobname(live_db, Source.ADMIN, "onsave")
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        ScheduledTask.objects.create(
            name="onsave", source=Source.ADMIN, task="tests.pg_cron.tasks.add",
            queue="default", cron="5 seconds",
        )
        assert utils.fetch_cron_job(jobname) is None  # NOT emitted before commit
    for callback in callbacks:
        callback()
    assert utils.fetch_cron_job(jobname) is not None  # emitted on commit
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_schedule_emission.py -v` Expected: FAIL on the
FIRST assertion — pre-`on_commit`, `post_save` still emits synchronously, so the job
exists inside the block (`fetch_cron_job(...) is None` fails). This discriminates: under
the old code emission happens too early; only the `on_commit` wiring makes it hold.

- [ ] **Step 3: Implement (prose)**

Rewrite `signals.schedule_job_on_save` / `unschedule_job_on_delete` to register
`transaction.on_commit` callbacks that call the catalog (the callback body
swallows-and-logs a post-commit central failure). Rewrite the module docstring's
emission contract (two connections; emission after commit; NO lock — concurrent writes
are idempotent upserts / `update_or_create` and self-heal at the next reconcile). In
`reconcile.py`, run the bulk body on the central connection in order: upsert declared
jobs → prune orphaned jobs (source + `WHERE database = <live>`) → schedule/unschedule
the cleanup job. Note explicitly in comments that lost row↔job atomicity is acceptable
because the run-wrapper re-reads the row each fire.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_schedule_emission.py tests/pg_cron/test_cross_source_coexistence.py tests/pg_cron/test_admin/ -v`
Expected: PASS (admin HTTP save/delete still emits via the commit hook).

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/signals.py django_absurd/pg_cron/reconcile.py tests/pg_cron/test_schedule_emission.py
/usr/bin/git commit -m "feat(pg_cron): emit schedules via transaction.on_commit; central-conn reconcile body"
```

---

### Task 7: Drop CreateExtension + central compose + move `tests/pg_cron` to an ordinary test DB

**Files:**

- Modify: `django_absurd/pg_cron/migrations/0001_initial.py:69` (remove
  `CreateExtension("pg_cron")`); **ROOT `compose.yaml:16-27` + ROOT
  `Dockerfile.pg_cron`** — the `db_pg_cron` service the suite uses: change
  `cron.database_name` OFF the test DB (`absurd_test_pg_cron`) to a central DB (e.g.
  `postgres`), add the extension + `USAGE ON SCHEMA cron` +
  `EXECUTE ON cron.schedule_in_database` grants via a `/docker-entrypoint-initdb.d` init
  script; `tests/pg_cron/settings.py` (drop the `TEST["NAME"] == cron.database_name` pin
  → ordinary `test_<name>`; set BOTH `SYNC_SCHEDULES_ON_TEST_DB` + `PG_CRON_ON_TEST_DB`
  — the central DB is auto-discovered, no option to set); `tests/pg_cron/utils.py`.
  Separately move the demo the same way: `examples/pg_cron/compose.yaml` +
  `examples/pg_cron/Dockerfile` (its `cron.database_name=demo` → central).
- Test: full `tests/pg_cron` suite must pass on the new topology (incl. `-n 2` xdist
  smoke)

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
removal; the `RunSQL` wrapper stays). Rework the ROOT `Dockerfile.pg_cron` / ROOT
`compose.yaml` (`db_pg_cron` service) so `cron.database_name` is a central DB (e.g.
`postgres`) with the extension + `USAGE ON SCHEMA cron` +
`EXECUTE ON cron.schedule_in_database` grants created once via an init script (runs only
on a fresh volume — call out that existing dev volumes need `docker compose down -v`
recreation). Change `tests/pg_cron/settings.py`: `DATABASES["default"]["TEST"]` becomes
an ordinary `test_<name>` (no pin to `cron.database_name`) and set both opt-in knobs —
the central DB is auto-discovered on the same server via
`current_setting('cron.database_name')`, no option to set. Update the CLAUDE.md
`tests/pg_cron` guidance in Task 10.

- [ ] **Step 4: Run the full suite (incl. xdist smoke)**

```bash
docker compose down -v && docker compose up -d db db_pg_cron   # ROOT compose — re-runs the init script on a fresh db_pg_cron volume
uv run pytest tests/pg_cron --create-db
uv run pytest tests/pg_cron -n 2   # xdist: workers migrate test_<db>_gwN with NO CREATE EXTENSION
```

Expected: PASS on both — no `--create-db` eviction dance, xdist green.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/migrations/0001_initial.py Dockerfile.pg_cron compose.yaml examples/pg_cron/ tests/pg_cron/settings.py tests/pg_cron/utils.py tests/pg_cron/test_migration_has_no_create_extension.py
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
from django.core.management.base import SystemCheckError

from tests.pg_cron import utils


def test_composition_check_rejects_sync_on_test_db_without_opt_in(
    settings: object,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True
    with pytest.raises(SystemCheckError) as excinfo:
        call_command("check", "django_absurd")
    assert "<the COMPLETE absurd.E0xx message text>" in str(excinfo.value)
    assert "<the COMPLETE hint text>" in str(excinfo.value)
```

Follow the suite's established check-test idiom (`test_scheduler_app_checks.py`):
`call_command("check", ...)` raises `SystemCheckError` with the messages on `str(exc)` —
NOT `SystemExit`/`capsys`. Assert the COMPLETE message + hint text (the placeholders
above are filled with the real strings the check emits).

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

### Task 9: Command inert guard + start-sweep fixture + runtime isolation test

**Files:**

- Modify: `django_absurd/pg_cron/management/commands/absurd_sync_crons.py`
  (`CommandError` when inert), `django_absurd/pytest_plugin.py` (session-scoped autouse
  start-sweep fixture)
- Test: `tests/pg_cron/test_absurd_sync_crons_command.py` (inert `CommandError`),
  `tests/pg_cron/test_isolation_regression.py` (the runtime no-leak test)

The structural sweep-scoping regression is already covered by Task 5's
`test_flush_scoped`, so there is no separate helper-level duplicate here. The
start-sweep fixture gets no dedicated test: it is session-autouse (runs before any
in-suite test can seed an orphan to observe), it just calls the already-tested
`flush_database_jobs`, and its lines are covered by executing every session.

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
# The runtime no-leak proof (the live experiment, made deterministic). NOT marked slow —
# it RUNS in CI (no deselected tests). Determinism comes from a positive sync point
# (poll cron.job_run_details until the producer fires into its own DB) rather than a
# blind sleep; xdist-safe because the producer targets a per-worker-unique DB.
import pytest
from django.db import connections

from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_producing_schedule_never_fires_into_this_test_db(settings: object) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    test_db = str(connections["default"].settings_dict["NAME"])
    # a task-producing cron bound to a NON-test DB (unique per worker), enqueuing each second
    producer = utils.schedule_producer_cron(target=utils.scratch_db_name())
    try:
        utils.wait_for_fire(producer, timeout=20)      # positive sync: it fired into its own DB
        assert utils.absurd_queue_depth(test_db) == 0  # nothing leaked into THIS test DB
    finally:
        utils.remove_producer(producer)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_isolation_regression.py -v`
Expected: FAIL — command doesn't raise; producer/poll helpers missing.

- [ ] **Step 3: Implement (prose)**

Guard `absurd_sync_crons.handle` with `is_pg_cron_inert` → `CommandError`. Add the
session-scoped autouse start-sweep fixture to `pytest_plugin.py` per the interface
(import-safe: only import `catalog` inside the fixture body, behind the `is_installed`
guard). Add the `utils.py` producer helpers, which own the scratch-DB lifecycle:
`scratch_db_name()` returns a per-worker-unique name (suffix with
`PYTEST_XDIST_WORKER`); `schedule_producer_cron(target)` **provisions** that DB
(`CREATE DATABASE` on a raw admin connection if absent, run the app migrations / install
the absurd SQL into it so `spawn_task` works) THEN schedules a `schedule_in_database`
job (unique jobname) enqueuing a task into `target` every second, returning a handle
carrying its jobid + target; `wait_for_fire(producer, timeout)` polls
`cron.job_run_details` for `status='succeeded'` on that jobid; `absurd_queue_depth(db)`
reads the test DB's queue; `remove_producer(producer)` unschedules the job AND drops the
scratch DB. The runtime test RUNS in CI — no `slow` marker, no deselection.

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_isolation_regression.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add django_absurd/pg_cron/management/commands/absurd_sync_crons.py django_absurd/pytest_plugin.py tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_isolation_regression.py tests/pg_cron/utils.py
/usr/bin/git commit -m "feat(pg_cron): inert command guard, session start-sweep fixture, runtime isolation test"
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
`schedule_in_database`; one scheduling role for the Absurd DB; the central DB is
auto-discovered (`current_setting('cron.database_name')`), so nothing to configure — and
same-DB deployments (where `cron.database_name` IS the app DB) keep working unchanged.
Build the site: `uvx zensical build` (expect "No issues found").

- [ ] **Step 2: Update contributor docs**

CLAUDE.md: remove the `--create-db` eviction procedure and the pinned-`TEST NAME`
requirement; describe `tests/pg_cron` running on an ordinary test DB against a central
`db_pg_cron`. Update `.claude/skills/pg-cron` "In this repo".

- [ ] **Step 3: Capture the why**

Run the `capture-why` skill to reverse the WHY.md "Extension in the app migration
(fail-fast)" section and capture the new durable rationale. Write it as _why_, not _how_
(no symbol/file names — must survive renames). The load-bearing points to capture:

- **Why the app/test DB never holds the extension.** `cron.database_name` is a single
  cluster-wide GUC and `CREATE EXTENSION pg_cron` is legal only in that one database. If
  the extension lived in the app DB, every `test_<db>` (and every xdist `test_<db>_gwN`)
  would have to BE that one database — which breaks pytest-xdist and standard `test_`
  isolation. So the extension is operator-managed once on a central metadata DB, and
  jobs run cross-DB via `schedule_in_database(database => <app db>)`. Fail-fast moved
  from a migrate-time `CREATE EXTENSION` to a deploy-time system check.
- **Why a plain test DB therefore has no `cron` schema — and why the seam is inert by
  default under tests.** Because a normal test DB never has the extension, any attempt
  to touch `cron.*` there would raise "schema cron does not exist". So the scheduling
  seam **gates itself off (no-ops) under tests by default** — common tests never connect
  to pg_cron and never error; they simply do nothing cron-related. Tests that genuinely
  need to exercise scheduling **opt in** (a per-backend option), which flips the seam
  live and routes it to the real central catalog.
- **Why reaching the central catalog uses a connection Django's test runner can't see.**
  The central metadata DB must be stable infrastructure, present with its extension. A
  second Django `DATABASES` alias would be captured by the test runner's database setup
  — created as an empty, extension-less `test_<name>` per run and per xdist worker —
  which would defeat the point. So the seam reaches the central DB through a short-lived
  connection derived from the app connection's own parameters (same server, zero extra
  config) with only the database name swapped, deliberately outside the test-DB
  lifecycle. This same path serves production and tests uniformly.
- **Why disabling a schedule cannot rely on the re-schedule call alone** (the
  `alter_job` retention): the cross-DB schedule call only honors the active flag when it
  first creates a job; re-scheduling an existing job ignores it. So an explicit
  set-active step is required to make disabling an existing schedule actually take
  effect — and in a deployment where the scheduling role does not own the central
  extension, that step needs its own grant (single-DB deployments, where the app role
  owns the extension, need nothing extra).

Do NOT run `archive-specs`.

- [ ] **Step 4: Cross-check + commit**

Verify command/flag/message/default copy matches code across README/AGENTS/site.

```bash
/usr/bin/git add django_absurd/AGENTS.md docs/web/ CLAUDE.md .claude/skills/pg-cron/SKILL.md docs/WHY.md
/usr/bin/git commit -m "docs: cross-database pg_cron scheduling (central DB, no app-DB extension)"
```

---

## Self-Review

**Spec coverage:** central metadata DB (T7) · central connection + B1 (T2) · one seam
(T4/T5) · drop length restriction (T3) · db-namespaced `build_jobname` in catalog + fold
`active` / drop `alter_job` (T4) · on_commit emission + reconcile rework (T6) · cleanup
job central (T5) · cleanup lifecycle teardown + session-start sweep (T5 teardown / T9
start) · scoped flush (T5) · backward compat degenerate case (T2 auto-discovery) ·
detection leaf + inert gate + opt-in (T1) · validate skip when inert (T4/T5 gate) ·
command CommandError (T9) · composition + `Tags.database` checks (T8) · structural
isolation regression (T5 `test_flush_scoped`) + runtime no-leak (T9) · migration drop +
compose + suite move (T7) · WHY/docs (T10). All spec sections mapped.

**Descoped (alpha + post-review cruft cut):** the transition sweep (B2), the
jobname-length cap, the two-verb cleanup API, the advisory lock, the
`CRON_DATABASE_NAME` option, the `PgCronManager` + `get_job`/`get_managed_jobs` read
surface, the duplicate fast isolation test, and the dedicated start-sweep test are all
cut. Rationale: alpha = no legacy jobs to migrate; `jobname` is unbounded `text`
(live-validated); auto-discovery is the only correct central-DB value;
emission-on_commit + self-heal make the lock a no-op footgun; the reads/cleanup verbs
had zero prod consumers.

**Placeholder scan:** NO `...` placeholder bodies remain — every step carries real test
code or concrete prose.

**Type consistency:** the single `build_jobname(database, source, name="")` is defined
in `catalog.py` (T4) and called only there; `validators.py` no longer owns it. Catalog
verbs all take `alias: str` first + keyword-only params, consistent across T4/T5/T9.
`is_pg_cron_inert(alias)` consumed identically in T4/T5/T8/T9.
`open_central_connection(alias)` / `resolve_cron_database(alias)` consistent T2→T4.
`utils.fetch_cron_job(jobname)` is the one test-side `cron.job` reader, used in
T4/T5/T6.

## Post-Implementation Validation

Run these AFTER Task 10, from a clean state, to prove the whole thing works the way a
downstream developer would exercise it. Each scenario is a real, runnable check —
automate them into a `scripts/validate_pg_cron.sh` (or a `make` target) so they're
repeatable, not a one-time manual dance. Every scenario must pass.

**Scenario 1 — fresh cluster from zero (the headline promise).**

```bash
docker compose down -v                       # wipe volumes — no leftover state
docker compose up -d db db_pg_cron           # db_pg_cron runs the init script: CREATE EXTENSION + grants on cron.database_name
uv run pytest tests/core                      # pg_cron NOT installed — must be green
uv run pytest tests/pg_cron                   # pg_cron installed, app/test DB has NO extension — must be green
uv run pytest tests/multidb                   # router suite — green
```

Expect: all three suites pass on a cluster that has the extension ONLY in the central
DB.

**Scenario 2 — pytest-xdist (the isolation win).**

```bash
uv run pytest tests/pg_cron -n 4              # each worker migrates test_<db>_gwN with NO CREATE EXTENSION
```

Expect: green — no `can only create extension in database ...` error on any worker. This
is the whole point; if it fails, the app DB is still touching `cron.*`.

**Scenario 3 — `--create-db` with no eviction dance.**

```bash
uv run pytest tests/pg_cron --create-db       # drops/recreates test DBs; the launcher holds no session on them
```

Expect: green with NO manual `ALTER DATABASE ... ALLOW_CONNECTIONS false` + terminate
step. (If this needs the old dance, the app DB is still the pg_cron database.)

**Scenario 4 — fresh migrate on a real (non-test) DB.**

```bash
# against a throwaway non-test DB on db_pg_cron, with a SCHEDULE + CLEANUP configured:
uv run python -m manage migrate               # reconcile runs; no CREATE EXTENSION on the app DB
uv run python -m manage absurd_sync_crons     # idempotent; second run makes no changes
uv run python -m manage check --database default   # central-extension E-check passes (extension present centrally)
```

Then inspect centrally: `SELECT jobname, database, active FROM cron.job` shows the
schedules + `_dj:<db>:c:cleanup_all`, all `database = <the app db>`.

**Scenario 5 — live scheduling actually fires into the app DB, not elsewhere.** Bring up
the `examples/pg_cron` app (`docker compose up --build` in that dir) and confirm its
scheduled task produces work in the app DB's queue and the launcher run history
(`cron.job_run_details.status='succeeded'`, `database = <app db>`).

**Scenario 6 — same-DB backward-compat (degenerate case).** On a cluster where
`cron.database_name` IS the app DB (an existing-style deployment), Scenario 4 still
works with zero config: auto-discovery returns the app DB, and
`schedule_in_database(database => current)` behaves exactly like the old
`cron.schedule`.

**Scenario 7 — full matrix (parity across versions).**

```bash
uvx --with tox-uv tox                          # Python × Django matrix + min-max mypy
```

If any scenario fails, that's a real defect — do not paper over it; trace it to the task
whose deliverable it exercises.

## Execution Handoff

Plan saved to `docs/plans/2026-07-24-pg-cron-cross-database-scheduling.md`. Two
execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between
   tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session with checkpoints.

Which approach?
