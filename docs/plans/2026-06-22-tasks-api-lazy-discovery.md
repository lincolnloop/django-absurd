# Lazy Task Discovery (SP4) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the worker's `tasks.py` scan with a `LazyTaskRegistry` that resolves
tasks by `module_path` via `import_string` on demand.

**Architecture:** Install a `LazyTaskRegistry` on the SDK client's `_registry` in
`worker_client`. Its `.get(name)` lazily `import_string`s the task and builds a handler;
the SDK's `_execute_task` (burst) and `start_worker` (blocking) both read the registry
via `.get`, so one mechanism serves both. Delete
`discover_tasks`/`register_tasks`/`collect_tasks_from_module` + `autodiscover_modules` +
the zero-tasks guard + the tasks.py contract.

**Tech Stack:** Django 6.0 `django.tasks`, absurd-sdk, psycopg3, pytest + pytest-django.

## Global Constraints

- A claimed task's name IS its dotted `module_path`; `import_string(module_path)`
  returns the `Task` (because `@task` binds it to that module-level name). Tasks run
  from ANY importable module — no tasks.py requirement.
- `LazyTaskRegistry.get(name)`: cache miss → `import_string(name)`; `ImportError` →
  return the `default` (SDK defers); not a `django.tasks.Task` → return `default`
  (defer); else cache + return
  `{"name": name, "queue": <worker queue>, "default_max_attempts": None, "default_cancellation": None, "handler": build_handler(task)}`.
  A module that EXISTS but raises on import lets the exception propagate (loud — real
  bug, not a silent defer).
- The registry's `queue` is the worker's queue, so the SDK's queue-mismatch guard never
  trips.
- Under `concurrency>1`, several pool threads may first-resolve the SAME uncached name
  concurrently (each runs `import_string` + `self[name]=…`). Benign under the GIL —
  idempotent import, atomic dict assignment, at worst a redundant `build_handler` — so
  NO lock is needed. (`test_start_worker_drains_concurrently` enqueues the same task 5×
  and exercises this.)
- Installed once in `worker_client` via `client._registry = LazyTaskRegistry(queue)` —
  ONE new `# noqa: SLF001` (the SDK has no public fallback-resolver hook). Total SLF001
  in `worker.py` becomes 3 (`_execute_task`, `ctx._task["attempt"]`, this).
- Keep: `build_handler` + `takes_context` bridge, `drain_queue`, `run_blocking_worker`,
  `WorkerOptions`, the command, `worker_client` connection lifecycle + provisioning
  check.
- Imports `import typing as t`; absolute imports; verb-named funcs; no
  leading-underscore module names; helpers below callers. No ruff ignores beyond the 3
  SLF001.
- Tests highest-level: drive `call_command("absurd_worker", queue=…, burst=True)` /
  `run_worker(burst=True)`; assert DB rows (`Group`) + Absurd result snapshots
  (`fetch_task_result`). pytest function-based,
  `@pytest.mark.django_db(transaction=True)`, no mocks.
- DB: `docker compose up -d db`; run with `PGPORT=5433`.

---

### Task 1: Replace the scan with `LazyTaskRegistry`

**Files:**

- Modify: `django_absurd/worker.py` (add `LazyTaskRegistry`; install it in
  `worker_client`; rewire `run_worker`; delete `discover_tasks`, `register_tasks`,
  `collect_tasks_from_module`; swap the `autodiscover_modules` import for
  `import_string`)
- Create: `tests/jobs.py` (a `@task` in a NON-`tasks.py` module — proves the contract is
  gone)
- Modify: `tests/test_worker.py` (add the tasks-anywhere test; delete
  `test_zero_tasks_for_alias_errors`; update the concurrency test + imports)

**Interfaces:**

- Consumes: `worker_client(backend, queue)`, `build_handler(task)`, `drain_queue`,
  `run_blocking_worker`, `WorkerOptions` (SP3, unchanged); `django.tasks.Task`;
  `django.utils.module_loading.import_string`.
- Produces: `LazyTaskRegistry(queue)` (a `dict` subclass overriding `.get`);
  `run_worker(backend, queue, *, burst=False, options=None) -> None` (no longer calls
  `register_tasks`). `discover_tasks`/`register_tasks`/`collect_tasks_from_module`
  REMOVED.

- [ ] **Step 1: Write the failing test (task outside tasks.py runs)**

Create `tests/jobs.py`:

```python
"""A task that lives OUTSIDE tasks.py — the lazy worker must still run it."""
from django.contrib.auth.models import Group
from django.tasks import task


@task
def record_from_jobs(name: str) -> str:
    Group.objects.get_or_create(name=name)
    return name
```

Add to `tests/test_worker.py` (alongside the other command-driven tests, using the
existing `run_absurd_worker` + `snapshot` helpers):

```python
def test_task_outside_tasks_py_runs():
    # record_from_jobs is in tests/jobs.py, NOT tests/tasks.py — the old scan would
    # never find it (it would defer forever). Lazy resolution runs it by module_path.
    from tests.jobs import record_from_jobs

    call_command("absurd_sync_queues")
    result = record_from_jobs.enqueue("from-jobs")
    run_absurd_worker()
    assert Group.objects.filter(name="from-jobs").exists()
    assert snapshot(result.id).result == "from-jobs"
```

- [ ] **Step 2: Run to verify it fails**

Run:
`PGPORT=5433 uv run pytest tests/test_worker.py::test_task_outside_tasks_py_runs -v`
Expected: FAIL — under the current `tasks.py` scan, `record_from_jobs` (in
`tests/jobs.py`) is never discovered/registered, so the worker defers it and it never
runs: `Group.objects.filter(name="from-jobs").exists()` is `False` (and/or the snapshot
isn't `completed`).

- [ ] **Step 3: Implement minimal (prose — no production code block)**

In `django_absurd/worker.py`:

- Replace the import `from django.utils.module_loading import autodiscover_modules` with
  `from django.utils.module_loading import import_string`.
- **Also remove the now-unused imports** `import sys` and `from django.apps import apps`
  — after deleting `discover_tasks`/`collect_tasks_from_module` they're referenced
  nowhere else (they were only used for `sys.modules` + `apps.get_app_configs()` in the
  scan), and ruff `select=["ALL"]` (F401) errors on unused imports. (Keep
  `from django.core.exceptions import ImproperlyConfigured` — still used by
  `worker_client`'s provisioning check.)
- Add a `LazyTaskRegistry(dict)` class (place it near the top, e.g. just below
  `WorkerOptions`, above `worker_client`): `__init__(self, queue: str)` stores
  `self.queue = queue` and calls `super().__init__()`. Override
  `get(self, name, default=None)`: if `name` not already a key,
  `try: task = import_string(name)` / `except ImportError: return default`; if
  `not isinstance(task, Task): return default`; else set
  `self[name] = {"name": name, "queue": self.queue, "default_max_attempts": None, "default_cancellation": None, "handler": build_handler(task)}`.
  Then `return super().get(name, default)`. Use `import typing as t` for the annotation
  (`t.Any`). (Do NOT catch broad exceptions — a module that errors on import should
  surface.)
- In `worker_client`, right after `client = Absurd(conn, queue_name=queue)`, install the
  registry:
  `client._registry = LazyTaskRegistry(queue)  # noqa: SLF001 -- SDK has no public fallback-resolver hook; install lazy import_string resolution`.
- Rewrite `run_worker` to drop discovery: remove the `count = register_tasks(...)` line;
  change the startup log to omit `tasks=%d` (log alias/queue/database/burst/concurrency
  only — drop the `count` arg). The `with worker_client(...) as client:` block goes
  straight to `if burst: drain_queue(...) else: run_blocking_worker(client, options)`.
- DELETE `discover_tasks`, `register_tasks`, and `collect_tasks_from_module` entirely.
- `build_handler` is now called by `LazyTaskRegistry`; it stays. Keep the two existing
  `# noqa: SLF001` (`_execute_task` in `drain_queue`, `ctx._task["attempt"]` in
  `read_sdk_attempt`).

- [ ] **Step 4: Run to verify it passes**

Run:
`PGPORT=5433 uv run pytest tests/test_worker.py::test_task_outside_tasks_py_runs -v`
Expected: PASS — `record_from_jobs` resolves via
`import_string("tests.jobs.record_from_jobs")` and runs.

- [ ] **Step 5: Update the suite for the removed scan**

- DELETE `test_zero_tasks_for_alias_errors` (the zero-tasks guard is gone — there's
  nothing to enumerate, so no such error).
- In `test_start_worker_drains_concurrently`, REMOVE the
  `register_tasks(client, "default", "default")` line — `worker_client` now installs the
  lazy registry, so `start_worker` resolves `tests.tasks.make_group` by `module_path` on
  its own. (Keep everything else: the thread, poll-until-`Group` count==5,
  `stop_worker`, `close_worker_client`.)
- Update the `from django_absurd.worker import (...)` block in `tests/test_worker.py`:
  remove `register_tasks` (deleted); keep `worker_client` (and `build_handler` is not
  imported by tests). `test_unregistered_name_defers_not_crashes` stays as-is — it now
  exercises the `ImportError → defer` path of the lazy registry.

- [ ] **Step 6: Run the full suites + gates**

Run: `PGPORT=5433 uv run pytest tests/test_worker.py -v` → all pass (incl. the new
tasks-anywhere test; zero-tasks test gone). Run: `PGPORT=5433 uv run pytest` → full
single-DB suite green. Run: `PGPORT=5433 uv run pytest tests/multidb` → green. Run:
`uv run ruff check django_absurd tests` → clean (`grep -n noqa django_absurd/worker.py`
→ exactly 3, all SLF001). Run: `uv run mypy django_absurd` → Success. Run:
`PGPORT=5433 uvx --with tox-uv tox` → full 6-env matrix green.

- [ ] **Step 7: Commit**

```bash
git add django_absurd/worker.py tests/jobs.py tests/test_worker.py
git commit -m "feat: lazy task discovery (resolve by module_path); drop tasks.py scan"
```

---

## Self-Review

**Spec coverage:** `LazyTaskRegistry` + its error contract (Step 3); install in
`worker_client` with the one SLF001 (Step 3); deletions of
`discover_tasks`/`register_tasks`/`collect_tasks_from_module`/`autodiscover`/zero-tasks
guard/tasks.py contract (Steps 3 + 5); `run_worker` log drops `tasks=count` (Step 3);
tasks-anywhere proof (Steps 1–4); existing command-driven tests as the safety net (Step
6). The `module-errors-on-import propagates` case is documented as the contract (Step 3
"do NOT catch broad exceptions") and consciously not given a dedicated test (would need
a deliberately-broken importable module that breaks collection); the
`ImportError → defer` path is covered by `test_unregistered_name_defers_not_crashes`.

**Type consistency:** `LazyTaskRegistry(queue: str)`, `.get(name, default=None)`
returning the registration dict / `default`,
`run_worker(backend, queue, *, burst=False, options=None) -> None`, and the registration
dict shape (`name`/`queue`/`default_max_attempts`/`default_cancellation`/`handler`)
match the SDK's `_registry` contract (verified) and SP3's
`build_handler(task)`/`worker_client`/`drain_queue` signatures.

**Placeholder scan:** no TBD/TODO; the test step carries full assertion code; the
implementation step is prose (no production code) per the no-coding-ahead rule. The
registry's exact dict shape + error branches are pinned from the verified SDK source.
