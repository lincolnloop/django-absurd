# Native-Async Worker (SP7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Follow TDD: failing test → run RED → minimal
> implementation → run GREEN → refactor → commit.

**Goal:** Replace the sync/thread `absurd_worker` with a native-asyncio worker that runs
BOTH sync (`def`) and async (`async def`) tasks, and flip
`AbsurdBackend.supports_async_task = True`.

**Architecture:** `run_worker(...)` stays a SYNC entry: it `validate_backend(...)`
(Django `@async_unsafe` — must be off the loop) then `asyncio.run(arun_worker(...))`. On
the loop, `aworker_client` opens a dedicated `psycopg.AsyncConnection` (`AsyncAbsurd`);
an async `LazyTaskRegistry` resolves tasks by `module_path`; the async handler `await`s
`async def` tasks on the loop and runs `def` tasks via
`loop.run_in_executor(ThreadPoolExecutor)` (with `close_old_connections`). One
`--concurrency` knob sizes both the SDK's loop concurrency and the executor pool.

**Tech Stack:** Django 6.0 `django.tasks`, absurd-sdk (`AsyncAbsurd`/`AsyncConnection`),
psycopg3 async, asyncio, pytest + pytest-django, real Postgres.

## Global Constraints

- `import typing as t` (never `from typing import X`); `import datetime as dt`; absolute
  imports; helpers BELOW callers; no leading-underscore module names; verb-named
  functions.
- ruff `select=["ALL"]` passes with ZERO new ignores/noqa (HARD rule — ask first). The
  worker keeps exactly the 3 justified `# noqa: SLF001` (the `_registry` swap,
  `_execute_task`, `ctx._task["attempt"]`). mypy (django-stubs) clean, no
  `# type: ignore`.
- pytest, function-based ONLY, NO mocks. `tests/test_worker.py` uses
  `pytestmark = pytest.mark.django_db(transaction=True)`.
- **CLI unchanged:**
  `absurd_worker --queue … [--alias …] [--burst] [--concurrency N] [--claim-timeout …] [--poll-interval …] [--batch-size …] [--worker-id …]`.
  The command imports only `WorkerOptions` + `run_worker` (both preserved).
- **C1:** `params.pop("cursor_factory", None)` before
  `psycopg.AsyncConnection.connect(**params, autocommit=True)` (Django's sync
  cursor_factory is fatal for async connect; live-verified). Sync `psycopg.connect` does
  NOT need the pop.
- **@async_unsafe:** `validate_backend` (→ `ensure_connection`) runs in the SYNC
  `run_worker` entry, NEVER on the loop. `get_connection_params()` is config-only,
  loop-safe.
- **jsonb:** loader registered on the worker's DEDICATED `AsyncConnection` only
  (claim-time `params` decode). NEVER on Django's shared connection. Result reads in
  tests use a DEDICATED sync conn + loader (not `build_absurd_client`, which returns raw
  JSON strings, nor `get_absurd_client`'s shared conn, which would re-poison
  `JSONField`).
- `loop.add_signal_handler(SIGINT/SIGTERM, client.stop_worker)` (`stop_worker` is sync).
  KEEP `import signal` for the constants; drop `signal.signal()`. (Raises
  off-main-thread/Windows — worker runs on the main thread under `asyncio.run`;
  acceptable.)
- DB: `PGPORT=5433 docker compose up -d db`; run with `PGPORT=5433`.

Spec: `docs/specs/2026-06-24-async-worker-design.md`.

---

### Task 1: Rewrite the worker async; keep the behavioral suite green; flip `supports_async_task`

**Files:**

- Modify: `django_absurd/worker.py` (rewrite to async per the spec's Architecture)
- Modify: `django_absurd/backends.py:30` (`supports_async_task = True`)
- Create: `tests/atasks.py` (async test tasks — kept OUT of `tests/tasks.py` so a RED
  run, where the flag is still `False`, doesn't break collection of the whole sync
  suite)
- Modify: `tests/test_worker.py` (repoint the `get_task_result` helper to a
  dedicated-conn read; rewrite the `worker_client` provisioning tests,
  `test_unregistered_name_defers_not_crashes` (I1), and
  `test_start_worker_drains_concurrently` (I2))

**Interfaces:**

- Consumes: `AsyncAbsurd`, `psycopg.AsyncConnection`; `django.db.connections`,
  `close_old_connections`;
  `django.tasks.{Task,TaskContext,TaskResult,TaskResultStatus}`;
  `django_absurd.connection.{validate_backend,register_jsonb_loader}`;
  `django_absurd.backends.AbsurdBackend`.
- Produces (worker.py public, signatures unchanged where they exist): `WorkerOptions`
  (unchanged); `run_worker(backend, queue, *, burst=False, options=None) -> None` (sync
  entry); `build_task_context`, `read_sdk_attempt` (unchanged). Internals are now async:
  `arun_worker`, `aworker_client`, async `LazyTaskRegistry`, async `build_handler`,
  async `drain_queue`, async `run_blocking_worker`.

- [ ] **Step 1: Write the failing test**

Create `tests/atasks.py`:

```python
from django.tasks import task


@task
async def aecho(value):
    return value
```

Add to `tests/test_worker.py` (uses the module `pytestmark` + the existing
`run_absurd_worker` helper):

```python
def test_async_task_runs_end_to_end():
    from tests.atasks import aecho

    call_command("absurd_sync_queues")
    r = aecho.enqueue("hi-async")
    run_absurd_worker()
    snap = get_task_result(r.id)
    assert snap.state == "completed"
    assert snap.result == "hi-async"
```

- [ ] **Step 2: Run to verify it fails**

Run:
`PGPORT=5433 uv run pytest tests/test_worker.py::test_async_task_runs_end_to_end -v`
Expected: FAIL — with `supports_async_task=False`, `@task async def aecho` raises
`InvalidTask` at decoration (import of `tests/atasks.py`), so the test errors on import
/ enqueue. (Confirms async is unsupported pre-rewrite.)

- [ ] **Step 3: Implement (prose — no production code block, per the no-coding-ahead
      rule)**

In `django_absurd/backends.py`: set `supports_async_task = True`.

Rewrite `django_absurd/worker.py` to the spec's Architecture
(`docs/specs/2026-06-24-async-worker-design.md`), keeping `WorkerOptions`,
`build_task_context`, `read_sdk_attempt`, and the 3 `# noqa: SLF001` unchanged:

- Imports: drop `from absurd_sdk import Absurd` → `from absurd_sdk import AsyncAbsurd`;
  add `import asyncio`, `import inspect`,
  `from concurrent.futures import ThreadPoolExecutor`,
  `from contextlib import asynccontextmanager`; keep `import signal` (constants only —
  drop `signal.signal()` use).
- `run_worker(backend, queue, *, burst=False, options=None) -> None` (SYNC entry,
  unchanged signature): `options = options or WorkerOptions()`;
  `validate_backend(backend.database)` (here, off the loop);
  `asyncio.run(arun_worker(backend, queue, burst=burst, options=options))`.
- `arun_worker(...)`: create `ThreadPoolExecutor(max_workers=options.concurrency)` (use
  as the loop's executor for sync tasks; shut down on exit, e.g.
  `with ThreadPoolExecutor(...) as executor:`);
  `async with aworker_client(backend, queue) as client:` then the startup `logger.info`
  (alias/queue/database/burst/concurrency) and
  `if burst: await drain_queue(client, …) else: await run_blocking_worker(client, options)`.
  Make `executor` reachable by the handler (e.g. a contextvar, or pass into
  `aworker_client`/the registry so handlers use it; choose the simplest that keeps
  `build_handler` testable).
- `aworker_client(backend, queue)` (`@asynccontextmanager`):
  `params = connections[backend.database].get_connection_params(); params.pop("cursor_factory", None)`;
  `conn = await psycopg.AsyncConnection.connect(**params, autocommit=True)`; `try:`
  `register_jsonb_loader(conn)`; `client = AsyncAbsurd(conn, queue_name=queue)`;
  `client._registry = LazyTaskRegistry(queue)  # noqa: SLF001 …`; provisioning check
  `await client.list_queues()` wrapped in the SAME
  `except (InvalidSchemaName, UndefinedTable, UndefinedFunction)` →
  `ImproperlyConfigured` (absent schema) + `if queue not in provisioned` →
  `ImproperlyConfigured` (same messages as today); `yield client`;
  `finally: await conn.close()`. Do NOT call `validate_backend` here.
- async `LazyTaskRegistry(dict)`: identical resolution logic to today (`.get(name)`:
  cache-miss → `import_string`, `ImportError`/non-`Task` → `default`, else cache the
  entry
  `{name, queue, default_max_attempts:None, default_cancellation:None, handler: build_handler(task)}`).
  `.get` itself stays a normal (sync) method — the SDK calls `_registry.get(name)`
  synchronously; only the `handler` it stores is async.
- async `build_handler(task) -> AsyncTaskHandler`: `async def handler(params, ctx)`:
  `args=params.get("args",[])`, `kwargs=params.get("kwargs",{})`,
  `attempt=read_sdk_attempt(ctx)`, start/timing + the same start/failed/completed
  `logger` lines. Dispatch: `if inspect.iscoroutinefunction(task.func):` →
  `result = await task.func(ctx_, *args, **kwargs)` (when `task.takes_context`,
  `ctx_ = build_task_context(task, ctx, args, kwargs)`) else
  `await task.func(*args, **kwargs)`. **Else (sync):** wrap a `call_sync()` that does
  `close_old_connections()` → `task.func([ctx_,] *args, **kwargs)` → (finally)
  `close_old_connections()`, and
  `result = await asyncio.get_running_loop().run_in_executor(executor, call_sync)`. Keep
  the
  `try/except Exception: logger.exception(...); raise / else: logger.info(...); return result`
  shape so the SDK records failures + retries.
- async
  `drain_queue(client, *, claim_timeout=120, batch_size=None, worker_id=None) -> int`:
  loop
  `claimed = await client.claim_tasks(batch_size or 1, claim_timeout, worker_id or "worker")`;
  break if empty;
  `for t_ in claimed: await client._execute_task(t_, claim_timeout)  # noqa: SLF001 …`;
  return count.
- async `run_blocking_worker(client, options)`: `loop = asyncio.get_running_loop()`;
  `loop.add_signal_handler(signal.SIGINT, client.stop_worker)` + `SIGTERM`;
  `try: await client.start_worker(worker_id=…, claim_timeout=…, concurrency=…, batch_size=…, poll_interval=…)`
  `finally:` `loop.remove_signal_handler(signal.SIGINT)` + `SIGTERM`.

In `tests/test_worker.py`, repoint the `get_task_result` helper to a DEDICATED sync read
(the sync worker_client it used is gone). New top-level helper:

```python
def get_task_result(task_id, queue="default"):
    from absurd_sdk import Absurd
    from django.db import connections

    from django_absurd.connection import register_jsonb_loader

    raw_task_id = str(task_id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        return Absurd(conn).fetch_task_result(raw_task_id, queue)
    finally:
        conn.close()
```

(hoist the imports to the top of `test_worker.py` per ruff PLC0415; shown inline here
only for locality). Then:

- Rewrite the `worker_client` provisioning tests
  (`test_worker_client_uses_dedicated_connection`, `_unprovisioned_queue_errors`,
  `_absent_schema_errors`, `_rejects_non_psycopg3`) against `aworker_client` driven via
  `asyncio.run` (e.g. an `asyncio.run(_enter())` helper that
  `async with aworker_client(...)`), OR fold the
  unprovisioned/absent-schema/non-psycopg3 cases into the command-level
  `ImproperlyConfigured→CommandError` tests
  (`test_command_maps_improperly_configured_to_commanderror`, plus a `--database sqlite`
  rejection variant) — keep the same assertions/messages.
- I1 `test_unregistered_name_defers_not_crashes`: replace
  `with worker_client(...) as client: client.spawn(...)` with
  `get_absurd_client("default").spawn("not.a.real.task", {"args": [], "kwargs": {}}, queue="default")`
  (sync, no worker internals), keep the burst run +
  `assert get_task_result(spawn["task_id"]).state != "failed"`.
- I2 `test_start_worker_drains_concurrently`: delete the
  `threading.Thread`/sync-`start_worker` scaffold; replace with the async-concurrency
  smoke (Task 2) OR a minimal blocking-mode drive here. (Simplest: move the concurrency
  assertion to Task 2's smoke and here just assert burst drains multiple enqueued sync
  tasks.)

- [ ] **Step 4: Run to verify it passes**

Run: `PGPORT=5433 uv run pytest tests/test_worker.py -v` → all pass (the new async-runs
test + every existing behavioral test green via the executor path).

- [ ] **Step 5: Full suite + gates**

Run: `PGPORT=5433 uv run pytest` → green. `PGPORT=5433 uv run pytest tests/multidb` →
green. Run: `uv run ruff check django_absurd tests` → clean
(`grep -rn "noqa" django_absurd/worker.py` → exactly the 3 SLF001). Run:
`uv run mypy django_absurd` → Success.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/worker.py django_absurd/backends.py tests/atasks.py tests/test_worker.py
git commit -m "feat: native-async worker (runs sync via executor + async on the loop); supports_async_task=True"
```

---

### Task 2: Lock the async + jsonb behavior matrix

**Files:**

- Modify: `tests/atasks.py` (add async tasks: raising, takes_context, async-ORM
  `Payload`, sleeper)
- Modify: `tests/tasks.py` (a sync `echo` if not already present, for the mixed-run
  test)
- Create: `tests/test_async_worker.py` (the matrix; reuses
  `run_absurd_worker`/`get_task_result` patterns — import them or duplicate the small
  helpers)

**Interfaces:**

- Consumes: the async worker (Task 1); `tests/models.py::Payload` (JSONField model,
  exists since SP6); `run_absurd_worker()` (burst) + `get_task_result(...)`
  (dedicated-conn read, Task 1).

- [ ] **Step 1: Write the failing tests**

Add to `tests/atasks.py`:

```python
from asyncio import sleep as asleep

from tests.models import Payload


@task
async def aboom():
    msg = "aboom"
    raise ValueError(msg)


@task(takes_context=True)
async def areport_attempt(context):
    return context.attempt


@task
async def acreate_payload(data):
    obj = await Payload.objects.acreate(data=data)
    return obj.pk


@task
async def asleeper(seconds):
    await asleep(seconds)
    return "slept"
```

Create `tests/test_async_worker.py` (function-based,
`pytestmark = pytest.mark.django_db(transaction=True)`); reuse
`run_absurd_worker`/`get_task_result` (import from `tests.test_worker` or redefine the
small helpers):

```python
import time

import pytest
from django.core.management import call_command

from tests.atasks import aboom, acreate_payload, aecho, areport_attempt, asleeper
from tests.models import Payload
from tests.tasks import echo  # sync echo
from tests.test_worker import get_task_result, run_absurd_worker

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "value",
    [None, 0, False, "", [], {}, {"nested": [1, 2, {"a": None, "b": "ünïçødé"}]}],
)
def test_async_return_value_round_trips(value):
    call_command("absurd_sync_queues")
    r = aecho.enqueue(value)
    run_absurd_worker()
    snap = get_task_result(r.id)
    assert snap.state == "completed"
    assert snap.result == value


def test_async_failure_recorded():
    call_command("absurd_sync_queues")
    from django_absurd.params import AbsurdSpawnParams

    r = aboom.enqueue(absurd_spawn_params=AbsurdSpawnParams(max_attempts=1))
    run_absurd_worker()
    assert get_task_result(r.id).state == "failed"


def test_async_takes_context_attempt_is_one():
    call_command("absurd_sync_queues")
    r = areport_attempt.enqueue()
    run_absurd_worker()
    assert get_task_result(r.id).result == 1


def test_async_orm_jsonfield_round_trips():
    call_command("absurd_sync_queues")
    r = acreate_payload.enqueue({"async": True, "y": {"z": None}})
    run_absurd_worker()
    pk = get_task_result(r.id).result
    assert Payload.objects.get(pk=pk).data == {"async": True, "y": {"z": None}}


def test_sync_and_async_in_one_worker_run():
    call_command("absurd_sync_queues")
    rs = echo.enqueue({"mixed": "sync"})
    ra = aecho.enqueue({"mixed": "async"})
    run_absurd_worker()
    assert get_task_result(rs.id).result == {"mixed": "sync"}
    assert get_task_result(ra.id).result == {"mixed": "async"}


def test_worker_does_not_poison_jsonfield_reads():
    # The worker's loader is on its dedicated AsyncConnection; a Django JSONField read
    # on the shared connection after a worker run must still decode (no SP6-style poison).
    call_command("absurd_sync_queues")
    aecho.enqueue("x")
    run_absurd_worker()
    obj = Payload.objects.create(data={"k": "v", "n": 7})
    assert Payload.objects.get(pk=obj.pk).data == {"k": "v", "n": 7}


def test_async_concurrency_is_not_serial():
    call_command("absurd_sync_queues")
    for _ in range(4):
        asleeper.enqueue(0.5)
    start = time.monotonic()
    run_absurd_worker(concurrency=4)  # burst with concurrency
    elapsed = time.monotonic() - start
    assert elapsed < 1.5  # 4 * 0.5s serial == 2.0s; concurrent is well under
```

(If `run_absurd_worker` doesn't yet accept `concurrency`, extend it to pass
`concurrency=` through
`call_command("absurd_worker", queue=…, burst=True, concurrency=…)` — the command
already has the flag.) Add a sync `echo` to `tests/tasks.py` if absent:
`@task def echo(value): return value`.

- [ ] **Step 2: Run to verify they fail / drive gaps**

Run: `PGPORT=5433 uv run pytest tests/test_async_worker.py -v` Expected: PASS against
the Task-1 worker IF Task 1 is complete and correct. Any FAIL pinpoints a Task-1 gap
(e.g. sync-task executor path, async-ORM, jsonb decode, concurrency) — fix in
`worker.py`, not by weakening the test. (The matrix mirrors the 7 live-verified probe
scenarios + async execution behaviors.)

- [ ] **Step 3: (only if a test fails) fix `worker.py`**

Address the specific gap in the async worker per the spec; re-run.

- [ ] **Step 4: Full suite + gates**

Run: `PGPORT=5433 uv run pytest` → green. `PGPORT=5433 uv run pytest tests/multidb` →
green. Run: `uv run ruff check django_absurd tests` → clean. `uv run mypy django_absurd`
→ Success.

- [ ] **Step 5: Commit**

```bash
git add tests/atasks.py tests/tasks.py tests/test_async_worker.py django_absurd/worker.py
git commit -m "test: lock async-worker behavior + jsonb matrix (sync/async, failure, takes_context, async ORM, concurrency)"
```

---

## Self-Review

**Spec coverage:** async worker rewrite + `supports_async_task=True` +
`validate_backend` in sync entry + C1 cursor_factory pop + dedicated `AsyncConnection` +
async registry/handler + executor for sync + signals + one-knob concurrency (Task 1,
Step 3); behavioral suite stays green + internal-helper adaptations (`get_task_result`
dedicated read, I1, I2) (Task 1, Steps 3–4); the 7 live-verified jsonb scenarios + async
failure/takes_context/async-ORM/mixed/concurrency (Task 2). Dropped sync bodies covered
by the rewrite. `aenqueue` native path correctly OUT of scope.

**Placeholder scan:** none — test steps carry full code; implementation is prose
referencing the spec's exact architecture (no production-code blocks, per the
no-coding-ahead rule); the `get_task_result` helper is shown because it's a TEST helper,
not production code.

**Type consistency:**
`aworker_client`/`arun_worker`/`drain_queue`/`run_blocking_worker`/`build_handler`/`LazyTaskRegistry`/`build_task_context`/`read_sdk_attempt`/`WorkerOptions`
names + `run_worker(backend, queue, *, burst, options) -> None` match the spec and the
preserved command import. `get_task_result(task_id, queue="default")` +
`run_absurd_worker(queue="default", concurrency=…)` consistent between Task 1 and
Task 2. The 3 `# noqa: SLF001` are preserved, no new ignores.
