# Load / perf harness — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Standalone `loadtest/` harness — persistent DB, three commands (`load_seed`,
`load_admin`, `load_drain`) — producing baseline numbers for admin-at-volume and worker
throughput.

**Architecture:** Standalone Django project in `loadtest/` against its own persistent
compose DB. `load_seed` spawns real template rows then clones them server-side into the
millions. `load_admin` walks admin changelists over HTTP, capturing timings + SQL +
`EXPLAIN (ANALYZE, BUFFERS)`. `load_drain` runs a matrix of `absurd_worker --burst`
subprocesses over a fixed backlog. Harness's own correctness is pytest-tested at tiny N
against a throwaway test DB on the same server.

**Tech stack:** Django 6.0, psycopg3, absurd-sdk 0.4.x, pytest + pytest-django, Docker
Compose.

Spec:
[`docs/specs/2026-08-02-load-test-harness.md`](../specs/2026-08-02-load-test-harness.md).

## Global constraints

- Floor Django 6.0 / Python 3.12; psycopg (v3) backend only.
- **Harness ships zero production code.** No edits under `django_absurd/`. Probe needing
  something the package doesn't expose = finding to report, not a patch.
- `loadtest/` is NOT excluded from mypy or ruff. `[tool.mypy] exclude` lists only
  `build/`, `dist/`, `.tox/`, `examples/` — do not add `loadtest`. mypy `strict = true`
  and ruff `select = ["ALL"]` apply.
- `loadtest/` IS excluded from coverage, for free. Root `[tool.coverage.run] source` is
  `["django_absurd", "tests"]`, so anything outside those is already unmeasured — same
  way `examples/` is (it carries its own separate config in
  `examples/web/pyproject.toml:25`). Keep it that way: don't add `loadtest` to `source`,
  and don't put `--cov` in `loadtest/pytest.toml`. The 100%-patch-coverage rule covers
  shipped code; a dev harness is not shipped.
- `import typing as t`, `import datetime as dt` — never `from typing import X`
  (ruff-enforced). Absolute imports only.
- Functions contain a verb (`seed_clone_rows`, not `clone_rows_helper` / `row_cloner`).
  No leading-underscore module constants or helpers. Helper functions live BELOW their
  caller.
- pytest **function-based only**, never class-based. Non-fixture helpers go in a
  `utils.py` module, imported module-qualified (`from loadtest.tests import utils`).
- No `unittest.mock.patch`, no monkeypatching our own code. Drive real entrypoints.
- Never add a ruff `noqa` or ignore without asking first.
- Gates before any commit: `uv run pre-commit run --all-files` (owns ruff + mypy +
  prettier). Never invoke `ruff`/`mypy` directly.
- Commit after every task. Never `git commit --amend`.

## File structure

| Path                                         | Responsibility                                                        |
| -------------------------------------------- | --------------------------------------------------------------------- |
| `loadtest/__init__.py`                       | Package marker (ruff INP001 needs it).                                |
| `loadtest/compose.yaml`                      | `db_load` service, `PGPORT_LOAD` (default 5436), named volume.        |
| `loadtest/settings.py`                       | Standalone settings: admin apps, `TASKS` → `AbsurdBackend`, 4 queues. |
| `loadtest/urls.py`                           | Admin URLconf only.                                                   |
| `loadtest/manage.py`                         | Entrypoint pinning `DJANGO_SETTINGS_MODULE=loadtest.settings`.        |
| `loadtest/tasks.py`                          | Workload tasks: sync + async twins, plus the execution-log write.     |
| `loadtest/models.py`                         | `ExecutionLog` — the duplicate-detection side table.                  |
| `loadtest/schema.py`                         | Clone override map + `information_schema` drift guard.                |
| `loadtest/results.py`                        | Results dir resolution + timestamped JSON/plan writer.                |
| `loadtest/management/commands/load_seed.py`  | Template → clone → ANALYZE.                                           |
| `loadtest/management/commands/load_admin.py` | Admin HTTP + SQL + EXPLAIN probe.                                     |
| `loadtest/management/commands/load_drain.py` | Worker-matrix drain probe.                                            |
| `loadtest/pytest.toml`                       | Suite config (`--confcutdir=..`, `--reuse-db`).                       |
| `loadtest/tests/`                            | The harness's own tests, tiny N.                                      |
| `loadtest/README.md`                         | How to run it.                                                        |

Split rationale: the drift guard (`schema.py`) and the results writer (`results.py`) are
each used by two commands, so they don't belong inside either. Each command file owns
one probe.

---

### Task 1: Scaffolding — project, compose, workload tasks

**Files:**

- Create: `loadtest/__init__.py`, `loadtest/compose.yaml`, `loadtest/settings.py`,
  `loadtest/urls.py`, `loadtest/manage.py`, `loadtest/tasks.py`, `loadtest/models.py`,
  `loadtest/pytest.toml`, `loadtest/README.md`, `loadtest/tests/__init__.py`,
  `loadtest/tests/conftest.py`
- Modify: `pyproject.toml` (root pytest `addopts`), `.gitignore`
- Test: `loadtest/tests/test_setup.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `loadtest.settings` (4 declared queues: `bulk`, `alpha`, `beta`, `gamma` —
  `bulk` carries the volume); `loadtest.tasks.burn_sync(payload: dict) -> int` and
  `loadtest.tasks.burn_async(payload: dict) -> int`, both writing one `ExecutionLog` row
  and returning the payload's `n`; `loadtest.models.ExecutionLog` with fields
  `task_id: UUIDField`, `pid: IntegerField`,
  `logged_at: DateTimeField(auto_now_add=True)`.

No `run_id` field. The SDK exposes no public accessor for the running run's id
(`context.task_result.id` gives the task id), and reaching into `ClaimedTask._task` is
barred by the zero-production-code constraint. Duplicate detection needs only `task_id`:
rows minus distinct task ids. A permanently-NULL column would be dead weight.

- [ ] **Step 1: Write the failing tests**

`loadtest/tests/test_setup.py`:

```python
import pytest
from django_absurd import models as absurd_models

from loadtest import models, tasks


def test_migrate_provisions_every_declared_queue() -> None:
    assert set(
        absurd_models.Queue.objects.values_list("queue_name", flat=True)
    ) == {"bulk", "alpha", "beta", "gamma"}


@pytest.mark.django_db(transaction=True)
def test_the_sync_workload_task_logs_one_execution(dj_absurd) -> None:
    with dj_absurd.freeze_time():
        tasks.burn_sync.using(queue_name="bulk").enqueue({"n": 3})
        dj_absurd.drain(queue="bulk")

    assert models.ExecutionLog.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_the_async_workload_task_logs_one_execution(dj_absurd) -> None:
    with dj_absurd.freeze_time():
        tasks.burn_async.using(queue_name="bulk").enqueue({"n": 3})
        dj_absurd.drain(queue="bulk")

    assert models.ExecutionLog.objects.count() == 1
```

Read `docs/web/testing.md` and `tests/CLAUDE.md` first — `dj_absurd`'s exact
drain/freeze API is authoritative there, and this plan's call shapes must be corrected
to match it rather than the reverse.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest loadtest/tests/test_setup.py -v` Expected: collection error —
`loadtest` does not exist.

- [ ] **Step 3: Bring up the load database**

Write `loadtest/compose.yaml` with one `db_load` service: `postgres:18`,
`POSTGRES_PASSWORD=postgres`, port `${PGPORT_LOAD:-5436}:5432`, a **named volume** for
`/var/lib/postgresql/data` (data must survive `docker compose down`), and the same
`pg_isready` healthcheck the root `compose.yaml` uses.

Run: `docker compose -f loadtest/compose.yaml up -d db_load`

- [ ] **Step 4: Write the project skeleton**

`loadtest/settings.py` mirrors `tests/settings.py` — same INSTALLED_APPS plus
`loadtest`, same MIDDLEWARE/TEMPLATES/DATABASE_ROUTERS, `TIME_ZONE = "UTC"`.
Differences: `PGPORT` defaults to `5436` and `PGDATABASE` to `absurd_load`;
`ROOT_URLCONF = "loadtest.urls"`;
`TASKS["default"]["QUEUES"] = ["bulk", "alpha", "beta", "gamma"]`; no `sqlite` alias; no
`TEST` name override (the harness's own tests get Django's default `test_` prefix).

`loadtest/models.py` defines `ExecutionLog` per the Interfaces block — a plain managed
model, no constraints (duplicates are the signal, so a unique constraint would hide the
thing being measured).

`loadtest/tasks.py` defines the two workload tasks. Each writes its `ExecutionLog` row
and returns `payload["n"]`. Keep the body cheap and DB-bound: the probe measures the
engine, not the task. The async twin does the same work through the async ORM API.

`loadtest/urls.py` wires `admin.site.urls` only. `loadtest/manage.py` is Django's
standard entrypoint with `DJANGO_SETTINGS_MODULE=loadtest.settings`.

`loadtest/pytest.toml` mirrors a suite config from `tests/*/pytest.toml`:
`DJANGO_SETTINGS_MODULE=loadtest.settings`, `--confcutdir=..`, `--reuse-db`,
`--strict-markers`.

`loadtest/tests/conftest.py` holds only an autouse `_enable_db(db)` fixture, matching
`tests/conftest.py`'s pattern. The `dj_absurd` fixture comes from the installed pytest
plugin — do not redefine it.

- [ ] **Step 5: Keep a bare root `pytest` collecting nothing**

Add `--ignore=loadtest` to `[tool.pytest.ini_options] addopts` in `pyproject.toml`,
alongside the existing three `--ignore=tests/*` entries. Without it, a bare root
`uv run pytest` starts collecting the harness and the intentional exit-code-5 invariant
breaks.

Add `loadtest/results/` to `.gitignore`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `PGPORT=5436 uv run pytest loadtest -v` Expected: 3 passed.

- [ ] **Step 7: Write `loadtest/README.md`**

Cover: what the harness is for, `docker compose -f loadtest/compose.yaml up -d db_load`,
`PGPORT=5436 python -m loadtest.manage migrate` (note that `migrate` provisions queues
via `post_migrate` — no sync command), and that it is a dev harness excluded from CI.

- [ ] **Step 8: Run gates and commit**

```bash
uv run pre-commit run --all-files
git add loadtest pyproject.toml .gitignore
git commit -m "test(loadtest): scaffold the load harness project and workload tasks"
```

---

### Task 2: `load_seed` — template, clone, drift guard

**Files:**

- Create: `loadtest/schema.py`, `loadtest/management/__init__.py`,
  `loadtest/management/commands/__init__.py`,
  `loadtest/management/commands/load_seed.py`
- Test: `loadtest/tests/test_load_seed.py`

**Interfaces:**

- Consumes: `loadtest.tasks.burn_sync`, `loadtest.models.ExecutionLog` (Task 1).
- Produces: management command `load_seed` with options `--queue` (repeatable; default
  all four declared queues), `--tasks N` (clone count for `bulk`), `--window DAYS`
  (default 30), `--truncate`; and
  `loadtest.schema.check_columns_match(table: str, expected: set[str], using: str) -> None`
  raising `CommandError` on mismatch.

Lopsided by design, per the spec: `bulk` gets `--tasks` clones; `alpha`/`beta`/`gamma`
get a fixed 1000 each, so the admin's UNION-ALL views have real arms of realistic
mismatched size. A single `--queue bulk` still works for a targeted reseed.

Checkpoint / Event / Wait rows come from the template phase only and are NOT cloned. The
plain `burn_*` tasks produce none of them, so the template phase additionally enqueues a
`burn_workflow` task (add it to `loadtest/tasks.py`) that writes a checkpoint, emits an
event, and awaits one — enough to give those three admin entities non-empty views
without bulking them.

Reference columns (from `django_absurd/migrations/0001_initial_0_4_0.sql:148-190`) —
`absurd.t_<q>`:
`task_id, task_name, params, headers, retry_strategy, max_attempts, cancellation, enqueue_at, first_started_at, state, attempts, last_attempt_run, completed_payload, cancelled_at, idempotency_key`.
`absurd.r_<q>`:
`run_id, task_id, attempt, state, claimed_by, claim_expires_at, available_at, wake_event, event_payload, started_at, completed_at, failed_at, result, failure_reason, created_at`.

`idempotency_key` is `text unique` on unpartitioned queues — every clone must set it
NULL, since NULLs don't collide under a unique index.

- [ ] **Step 1: Write the failing tests**

`loadtest/tests/test_load_seed.py`:

```python
import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django_absurd import models as absurd_models

pytestmark = pytest.mark.django_db(transaction=True)


def test_seed_creates_the_requested_number_of_task_rows() -> None:
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)

    assert absurd_models.Task.objects.filter(queue="bulk").count() == 200


def test_every_seeded_task_gets_a_distinct_id() -> None:
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)

    rows = absurd_models.Task.objects.filter(queue="bulk")
    assert rows.values("task_id").distinct().count() == rows.count()


def test_seeded_tasks_spread_across_the_requested_window() -> None:
    call_command("load_seed", queue=["bulk"], tasks=500, window=30, truncate=True)

    times = list(
        absurd_models.Task.objects.filter(queue="bulk").values_list(
            "enqueue_at", flat=True
        )
    )
    assert (max(times) - min(times)).days >= 20


def test_every_seeded_run_points_at_a_seeded_task() -> None:
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)

    task_ids = set(
        absurd_models.Task.objects.filter(queue="bulk").values_list(
            "task_id", flat=True
        )
    )
    run_task_ids = set(
        absurd_models.Run.objects.filter(queue="bulk").values_list(
            "task_id", flat=True
        )
    )
    assert run_task_ids <= task_ids


def test_seed_refuses_to_clone_a_table_with_an_unknown_column() -> None:
    with connection.cursor() as cur:
        cur.execute("alter table absurd.t_bulk add column surprise_col text")

    with pytest.raises(CommandError, match="surprise_col"):
        call_command("load_seed", queue=["bulk"], tasks=10, truncate=True)


def test_truncate_clears_previously_seeded_rows() -> None:
    call_command("load_seed", queue=["bulk"], tasks=50, truncate=True)
    call_command("load_seed", queue=["bulk"], tasks=10, truncate=True)

    assert absurd_models.Task.objects.filter(queue="bulk").count() == 10
```

Note the last-but-one test performs DDL on a shared table — give the module the
`_isolate_queues` fixture (`tests/conftest.py`) via a module-level
`pytest.mark.usefixtures("_isolate_queues")` so the altered table can't leak into
another test under `--reuse-db`.

- [ ] **Step 2: Run to verify failure**

Run: `PGPORT=5436 uv run pytest loadtest/tests/test_load_seed.py -v` Expected: every
test errors — `Unknown command: 'load_seed'`.

- [ ] **Step 3: Write the drift guard**

`loadtest/schema.py` queries `information_schema.columns` for the given table and raises
`CommandError` naming any column present in the DB but absent from the expected set (and
vice versa). Message states the problem and the fix, per the project's msg/hint
convention: unknown column `x` on `absurd.t_bulk`, update `loadtest/schema.py`'s
override map. No silent tolerance — an unrecognised column is exactly the drift this
guard exists to catch.

- [ ] **Step 4: Write the seeder**

Three phases in `load_seed.py`:

1. `--truncate` (when passed) empties the queue's `t_`/`r_` tables and `ExecutionLog`.
2. Template — enqueue a handful of `burn_sync` tasks on the target queue, then drain
   them with a burst worker so rows exist in real terminal shapes. Leave a couple
   unenqueued- drained so a `pending` template exists too.
3. Clone — call the guard for both tables, then issue chunked
   `INSERT ... SELECT ... FROM absurd.t_<q> tmpl, generate_series(1, :chunk)`,
   overriding `task_id` with a fresh uuidv7, timestamps jittered across `--window`, and
   `idempotency_key` NULL. Clone `r_<q>` in the same transaction, keying `task_id` to
   the task ids just written and `run_id` fresh. Chunk at ~100k with progress written to
   stdout.
4. `ANALYZE` every touched table before returning.

Identifier interpolation goes through `psycopg.sql.Identifier` — the queue name reaches
SQL as a table name, and `django_absurd/admin_views.py` already models this pattern.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PGPORT=5436 uv run pytest loadtest/tests/test_load_seed.py -v` Expected: 6 passed.

- [ ] **Step 6: Run gates and commit**

```bash
uv run pre-commit run --all-files
git add loadtest
git commit -m "test(loadtest): add load_seed with a schema drift guard"
```

---

### Task 3: `load_admin` — timings, SQL, EXPLAIN

**Files:**

- Create: `loadtest/results.py`, `loadtest/management/commands/load_admin.py`
- Test: `loadtest/tests/test_load_admin.py`

**Interfaces:**

- Consumes: `load_seed` (Task 2) for data.
- Produces: management command `load_admin` with options `--entity` (repeatable; default
  all five), `--deep-page` (default 500); and
  `loadtest.results.write_run(name: str, payload: dict[str, t.Any]) -> pathlib.Path`
  returning the written JSON path under `loadtest/results/`.

**Results payload — fixed contract, both probes.** `write_run` writes
`{"name": str, "created_at": <ISO 8601 str>, "entries": [...]}`. `load_admin`'s
`handle()` returns the JSON path as a string (so `call_command` returns it and the path
also lands on stdout), and each entry is:

```json
{
  "entity": "tasks",
  "label": "unfiltered",
  "url": "/admin/django_absurd/task/",
  "status": 200,
  "elapsed_ms": 812.4,
  "queries": 4,
  "count_plan_path": "tasks-unfiltered-count.txt",
  "page_plan_path": "tasks-unfiltered-page.txt"
}
```

`label` is one of `unfiltered`, `queue`, `state`, `deep-page`. Plan paths are relative
to the results dir.

Probe matrix per entity: unfiltered page 1, `?queue=bulk`, `?state=completed`, and deep
page `?p=<deep-page>`.

**Plan defect, corrected after the fact:** this matrix prescribes a deep page for every
entity while the seeder scope above gives checkpoints/events/waits token rows only.
Django's `ChangeList.get_results` ignores `p` unless the result set actually paginates,
so on those three entities the deep-page probe answers 200 and records what is really an
unfiltered page-1 render. The probe must skip (or fail loudly on) a deep-page arm when
the entity does not paginate — the same shape as the state-arm skip for events/waits.
Admin URLs via `django.urls.reverse("admin:django_absurd_<model>_changelist")` — never
hand-written paths.

Two facts the probe must respect, both already true in
`django_absurd/admin.py:ReadOnlyAbsurdAdmin`: `ordering = ("natural_key",)` (an
expression sort — the prime suspect) and `show_full_result_count = False` (so the
expected COUNT profile is pagination's count alone, not Django's second full count).

- [ ] **Step 1: Write the failing tests**

`loadtest/tests/test_load_admin.py`:

```python
import json
import pathlib

import pytest
from django.core.management import CommandError, call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def seeded(admin_user: object) -> None:
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)


def run_probe(**options: object) -> dict:
    path = pathlib.Path(call_command("load_admin", **options))
    return json.loads(path.read_text())


def test_admin_probe_records_one_entry_per_probed_url(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2)

    assert [e["label"] for e in payload["entries"]] == [
        "unfiltered",
        "queue",
        "state",
        "deep-page",
    ]


def test_admin_probe_captures_both_query_plans_for_every_entry(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2)
    results_dir = pathlib.Path(payload["results_dir"])

    for entry in payload["entries"]:
        count_plan = (results_dir / entry["count_plan_path"]).read_text()
        page_plan = (results_dir / entry["page_plan_path"]).read_text()
        assert "cost=" in count_plan
        assert "cost=" in page_plan


def test_admin_probe_records_a_duration_and_query_count(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2)

    assert all(e["elapsed_ms"] > 0 for e in payload["entries"])
    assert all(e["queries"] >= 1 for e in payload["entries"])


def test_admin_probe_probes_every_entity_by_default(seeded: None) -> None:
    payload = run_probe(deep_page=2)

    assert {e["entity"] for e in payload["entries"]} == {
        "tasks",
        "runs",
        "checkpoints",
        "events",
        "waits",
    }


def test_admin_probe_fails_loudly_when_a_page_does_not_return_200(
    seeded: None,
) -> None:
    with connection.cursor() as cur:
        cur.execute("drop view absurd.tasks_view")

    with pytest.raises(CommandError, match="tasks"):
        run_probe(entity=["tasks"], deep_page=2)
```

`payload["results_dir"]` is part of the contract — `write_run` records the absolute
results directory alongside `entries` so plan paths resolve.

The last test performs DDL on a shared view, so give this module
`pytest.mark.usefixtures("_isolate_queues")` too. If dropping the view turns out to
yield a 200 (the admin degrades to an empty changelist when a view is missing — see
`ReadOnlyAbsurdAdmin.get_queryset`), then the probe has nothing to fail on: delete this
test rather than inventing a failure mode. Verify against the real admin before writing
the implementation.

- [ ] **Step 2: Run to verify failure**

Run: `PGPORT=5436 uv run pytest loadtest/tests/test_load_admin.py -v` Expected: errors —
`Unknown command: 'load_admin'`.

- [ ] **Step 3: Write the results writer**

`loadtest/results.py` resolves `loadtest/results/`, creates it if absent, and writes
`<name>-<UTC timestamp>.json`. Timestamp comes from `django.utils.timezone.now()`.
Returns the path. Plans are long — write them as sibling `.txt` files referenced by path
from the JSON rather than embedding megabytes of text inline.

- [ ] **Step 4: Write the probe**

`load_admin.py`:

1. Ensure a superuser exists, then `django.test.Client().force_login(...)` — the login
   form isn't what's being measured.
2. For each probe URL: reset `connection.queries_log`, time the `client.get`, assert
   200, record elapsed ms, query count, and the slowest queries.
3. Identify the changelist's COUNT and paged SELECT from the captured SQL, re-run each
   under `EXPLAIN (ANALYZE, BUFFERS)`, and store the plan text.
4. Print a readable stdout table; write JSON + plan files via `results.write_run`.

`connection.queries` only populates under `DEBUG = True` or
`django.test.utils.CaptureQueriesContext` — use the latter, it works regardless of
`DEBUG` and doesn't lie about production settings.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PGPORT=5436 uv run pytest loadtest/tests/test_load_admin.py -v` Expected: all
pass.

- [ ] **Step 6: Run gates and commit**

```bash
uv run pre-commit run --all-files
git add loadtest
git commit -m "test(loadtest): add the load_admin timing and EXPLAIN probe"
```

---

### Task 4: `load_drain` — worker matrix

**Files:**

- Create: `loadtest/management/commands/load_drain.py`
- Test: `loadtest/tests/test_load_drain.py`

**Interfaces:**

- Consumes: `loadtest.tasks.burn_sync` / `burn_async`, `loadtest.models.ExecutionLog`
  (Task 1); `loadtest.results.write_run` (Task 3).
- Produces: management command `load_drain` with options `--tasks N` (backlog per cell,
  default 1000), `--cell WxC` (repeatable, e.g. `4x4`; default `1x1 1x4 4x1 4x4`),
  `--workload` (`sync`/`async`/`both`; default `both`). Like `load_admin`, `handle()`
  returns the results JSON path as a string. Each entry:

```json
{
  "cell": "4x4",
  "workers": 4,
  "concurrency": 4,
  "workload": "sync",
  "tasks": 1000,
  "elapsed_s": 12.7,
  "tasks_per_sec": 78.7,
  "executions": 1004,
  "distinct_tasks": 1000,
  "duplicates": 4
}
```

Per cell: truncate the queue + `ExecutionLog`, enqueue N tasks through the real backend,
spawn W `absurd_worker --burst --concurrency C` subprocesses, wait for all to exit,
record wall-clock, tasks/sec, `ExecutionLog` row count, and distinct `task_id` count.
Rows minus distinct ids = duplicate executions.

`--burst` drains then exits (`django_absurd/management/commands/absurd_worker.py:57`),
so the subprocesses terminate on their own — no kill logic and no poll loop needed.
There is no `timeout` binary on this machine; if a bound is ever needed, use
`subprocess.wait(timeout=...)`.

Subprocess environment: children connect by env, so pass the **live test database name**
(`django.db.connection.settings_dict["NAME"]`) as `PGDATABASE`, plus `PGPORT` and
`DJANGO_SETTINGS_MODULE=loadtest.settings`. Tests must be
`@pytest.mark.django_db(transaction=True)` — a child process cannot see rows sitting in
an uncommitted test transaction.

- [ ] **Step 1: Write the failing tests**

`loadtest/tests/test_load_drain.py`:

```python
import json
import pathlib

import pytest
from django.core.management import call_command

from loadtest import models

pytestmark = pytest.mark.django_db(transaction=True)


def run_drain(**options: object) -> dict:
    path = pathlib.Path(call_command("load_drain", **options))
    return json.loads(path.read_text())


def test_drain_reports_one_entry_per_requested_cell() -> None:
    payload = run_drain(tasks=20, cell=["1x1", "1x2"], workload="sync")

    assert [e["cell"] for e in payload["entries"]] == ["1x1", "1x2"]
    assert [e["concurrency"] for e in payload["entries"]] == [1, 2]


def test_drain_executes_every_enqueued_task() -> None:
    run_drain(tasks=20, cell=["1x1"], workload="sync")

    assert models.ExecutionLog.objects.values("task_id").distinct().count() == 20


def test_drain_reports_a_positive_throughput() -> None:
    payload = run_drain(tasks=20, cell=["1x1"], workload="sync")

    entry = payload["entries"][0]
    assert entry["elapsed_s"] > 0
    assert entry["tasks_per_sec"] > 0


def test_drain_derives_duplicates_from_executions_minus_distinct_tasks() -> None:
    payload = run_drain(tasks=20, cell=["1x1"], workload="sync")

    entry = payload["entries"][0]
    assert entry["distinct_tasks"] == 20
    assert entry["duplicates"] == entry["executions"] - entry["distinct_tasks"]


def test_drain_runs_both_workloads_when_asked() -> None:
    payload = run_drain(tasks=10, cell=["1x1"], workload="both")

    assert [e["workload"] for e in payload["entries"]] == ["sync", "async"]
```

Note the duplicate test asserts the derivation, not `duplicates == 0`. At-least-once
delivery means a stray redelivery is legal; a test demanding zero would be flaky and
would also be asserting the wrong thing — the harness's job is to _count_ duplicates
honestly, not to prove there are none.

- [ ] **Step 2: Run to verify failure**

Run: `PGPORT=5436 uv run pytest loadtest/tests/test_load_drain.py -v` Expected: errors —
`Unknown command: 'load_drain'`.

- [ ] **Step 3: Write the drain probe**

Per the Interfaces block. Use `sys.executable -m loadtest.manage absurd_worker ...` so
the child runs the same interpreter. Report a stdout table (cell, workload, tasks/sec,
wall-clock, duplicates) and write the JSON via `results.write_run`.

Cells run sequentially, never concurrently — two cells sharing the DB would measure each
other.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PGPORT=5436 uv run pytest loadtest/tests/test_load_drain.py -v` Expected: all
pass.

- [ ] **Step 5: Extend the README**

Document the full run sequence: `up -d db_load` → `migrate` → `load_seed` → `load_admin`
/ `load_drain`, with the flag defaults and where results land.

- [ ] **Step 6: Run gates and commit**

```bash
uv run pre-commit run --all-files
git add loadtest
git commit -m "test(loadtest): add the load_drain worker-concurrency matrix"
```

---

## After the plan

Baseline capture is a separate session, not a task here: seed millions of rows, run both
probes, read the plans. Findings — including anything the harness couldn't reach without
a package change — go to issue #112.

Out of scope, per spec: sustained-rate producer, soak/leak, SIGKILL chaos, clock skew,
durable-sleep fan-out, pg_cron fan-out, all security work.
