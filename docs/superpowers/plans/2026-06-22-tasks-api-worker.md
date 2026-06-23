# Tasks-API Worker (SP3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `manage.py absurd_worker` — a sync process that claims tasks from one queue,
resolves each to its `@task` function, runs it, and lets the SDK record
completion/failure to Absurd.

**Architecture:** A dedicated autocommit psycopg connection spawned from Django's
`DATABASES` config (not the shared request connection).
`autodiscover_modules("tasks")` + a re-walk of `sys.modules` finds `@task` objects for
the alias; each is registered under the worker's queue with an adapter that calls
`task.func`. The SDK's `start_worker` runs the loop (threadpool when `--concurrency>1`).
No Django-side result writeback.

**Tech Stack:** Django 6.0 `django.tasks`, absurd-sdk
(`spawn`/`claim_tasks`/`work_batch`/`start_worker`/`stop_worker`/`register_task`),
psycopg3, pytest + pytest-django.

## Global Constraints

- Sync only. `AbsurdBackend` already sets
  `supports_async_task=False`/`supports_get_result=False` (SP1/SP2) — unchanged.
- **Delivery is at-least-once:** handler ORM commits independently of Absurd's
  `complete_run`; a crash between them re-runs the task. (Idempotency deferred.)
- **Dedicated connection, autocommit.** Spawn from
  `connections[backend.database].get_connection_params()` (psycopg-ready; pass straight
  to `psycopg.connect`, no stripping) with `autocommit=True` passed explicitly.
  Autocommit is a **correctness precondition** for `concurrency>1` (the SDK's threadpool
  shares this one connection for bookkeeping; psycopg3 `threadsafety==2` serializes ops,
  but without autocommit they'd share one transaction).
- **Discovery filters on backend alias ONLY** (`task.backend == alias`), NOT on queue;
  register every such task under the worker's `--queue`
  (`register_task(task.module_path, queue=queue)`). Contract: **tasks must live in an
  installed app's `<app>/tasks.py`**. Zero tasks discovered → error at startup.
- **`takes_context` bridge:** `TaskResult.attempts` is a read-only property
  `== len(worker_ids)`; the Absurd ctx exposes `ctx.task_id` + `ctx._task["attempt"]`
  (1-based). Build the `TaskResult` with `worker_ids=["absurd"] * ctx._task["attempt"]`
  and **no** `attempts=` kwarg.
- **Command:** `--queue` REQUIRED (validated against `backend.queues`, error lists
  valid); `--alias` auto-resolves the sole `AbsurdBackend` (errors listing aliases
  if >1); the five `start_worker` tunables exposed as flags with SDK defaults,
  `--worker-id` default `None` (SDK synthesizes `<host>:<pid>`).
- **Observability:** per-task log lines (name, id, attempt, outcome, duration) via a
  `django_absurd` logger; startup log (alias/queue/db/concurrency/registered count).
  Exit non-zero on fatal startup error, 0 on clean drain.
- Imports `import typing as t`; absolute imports; verb-named functions; no
  leading-underscore module names; helpers BELOW callers. No ruff ignores without asking
  — fix the code.
- **Testing is highest-level**: drive `run_worker` / the SDK `work_batch()` / the
  `absurd_worker` command and assert observable outcomes — DB rows, Absurd result
  snapshots (`client.fetch_task_result`), emitted message text, raised exceptions. Do
  NOT unit-test `discover_tasks`/`build_handler` internals. pytest function-based,
  `@pytest.mark.django_db(transaction=True)`, real Postgres, no mocks. Handler ORM tests
  use `django.contrib.auth.models.Group` (already installed; no new model/migration).
  Each enqueue/worker test runs `call_command("absurd_sync_queues")` first (autouse
  fixture drops queues per-test).
- DB: `docker compose up -d db` (wait for `pg_isready`); run with `PGPORT=5433`.

---

### Task 1: Worker connection — shared jsonb-loader helper + `open_worker_client`

**Files:**

- Modify: `django_absurd/queues.py` (extract the jsonb-loader registration into a shared
  verb-named helper; `get_absurd_client` calls it)
- Create: `django_absurd/worker.py` (`open_worker_client`)
- Test: `tests/test_worker.py` (new)

**Interfaces:**

- Consumes: `validate_backend`, `BACKEND_ERROR_MESSAGE`,
  `AbsurdBackend.{database,queues}`, `get_absurd_backends` (SP1/SP2).
- Produces:

  - `queues.py`: `register_jsonb_loader(raw_conn) -> None` — registers `json.loads` on
    the connection (the SP2 fix, now shared). `get_absurd_client` calls it instead of
    inlining `set_json_loads`.
  - `worker.py`:
    `open_worker_client(backend: AbsurdBackend, queue: str) -> tuple[Absurd, psycopg.Connection]`
    — asserts psycopg3 (`validate_backend`), opens a dedicated `autocommit=True` psycopg
    connection from `connections[backend.database].get_connection_params()`, registers
    the jsonb loader, validates the queue is provisioned (else closes the conn and
    raises `ImproperlyConfigured` naming the queue + `absurd_sync_queues`/`migrate`),
    and **returns BOTH** `Absurd(conn, queue_name=queue)` and the `conn` (so callers can
    close the socket without touching SDK internals).
  - `worker.py`: `close_worker_client(client, conn) -> None` — `client.close()` (stops
    the worker) then `conn.close()` (closes the socket). **Required because
    `Absurd(conn)` sets `_owned_conn=False`, so `client.close()` is a no-op on the
    socket** — without closing `conn` we leak a connection per client. Returning `conn`
    from `open_worker_client` is why this needs NO private (`client._conn`) access.
    Every caller (tests, `run_worker`'s `finally`) uses this.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worker.py`:

```python
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from django_absurd.queues import get_absurd_backends
from django_absurd.worker import close_worker_client, open_worker_client

pytestmark = pytest.mark.django_db(transaction=True)


def backend():
    return get_absurd_backends()["default"]


def test_open_worker_client_uses_dedicated_connection():
    call_command("absurd_sync_queues")
    from django.db import connections

    client, conn = open_worker_client(backend(), "default")
    try:
        assert conn is not connections["default"].connection
        assert conn.autocommit is True
    finally:
        close_worker_client(client, conn)


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_open_worker_client_rejects_non_psycopg3(settings):
    settings.TASKS = {"default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"DATABASE": "sqlite"},
    }}
    with pytest.raises(ImproperlyConfigured):
        open_worker_client(backend(), "default")


def test_open_worker_client_unprovisioned_queue_errors(settings):
    # Sync only "default"; then declare an extra queue that was never synced and
    # open the worker on it -> provisioning check fails.
    call_command("absurd_sync_queues")
    settings.TASKS = {"default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default", "unsynced"],
        "OPTIONS": {"DATABASE": "default"},
    }}
    with pytest.raises(ImproperlyConfigured) as exc:
        open_worker_client(backend(), "unsynced")
    message = str(exc.value)
    assert "unsynced" in message
    assert "absurd_sync_queues" in message


def test_open_worker_client_absent_schema_errors():
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(ImproperlyConfigured, match="migrate"):
            open_worker_client(backend(), "default")
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)
```

- [ ] **Step 2: Run to verify failures**

Run: `PGPORT=5433 uv run pytest tests/test_worker.py -v` Expected: FAIL —
`django_absurd.worker` / `open_worker_client` does not exist.

- [ ] **Step 3: Implement minimal (prose — no production code block)**

- In `queues.py`, add `register_jsonb_loader(raw_conn)` that calls
  `set_json_loads(json.loads, raw_conn)` (carry the existing explanatory comment to it).
  Replace the inline call in `get_absurd_client` with a call to this helper. Behavior
  unchanged.
- Create `django_absurd/worker.py`. Implement `open_worker_client(backend, queue)`: call
  `validate_backend(backend.database)` (raises `ImproperlyConfigured` on non-psycopg3);
  get `connections[backend.database].get_connection_params()`; open a psycopg connection
  from those params with `autocommit=True` (keep a local `conn` ref);
  `register_jsonb_loader(conn)`; build `client = Absurd(conn, queue_name=queue)`. Then
  validate the queue is provisioned: list the queues (e.g. `client.list_queues()`)
  wrapped in the SP2-style catch — if the absurd schema is absent
  (`InvalidSchemaName`/`UndefinedTable`/`UndefinedFunction`) OR the queue isn't among
  the provisioned queues, **`conn.close()`** and raise `ImproperlyConfigured` naming
  `queue` and pointing at `manage.py absurd_sync_queues` (and `migrate` when the schema
  is absent — the message must contain "migrate" for that case). Otherwise
  `return client, conn`. Use `import typing as t`; absolute imports.
- Implement `close_worker_client(client, conn)`: `client.close()` (stops the worker)
  then `conn.close()` (the SDK's `close()` is a no-op on the socket because
  `Absurd(conn)` sets `_owned_conn=False`; closing the `conn` we returned from
  `open_worker_client` needs no private access). Place it BELOW `open_worker_client`.

- [ ] **Step 4: Run to verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_worker.py -v` Expected: PASS (4 tests). Run:
`PGPORT=5433 uv run pytest` · `uv run ruff check django_absurd tests` Expected: whole
single-DB suite green (the loader extraction is behavior-preserving — enqueue/claim
tests still pass); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/queues.py django_absurd/worker.py tests/test_worker.py
git commit -m "feat: open_worker_client (dedicated autocommit conn) + shared jsonb loader"
```

---

### Task 2: Dispatch + burst run — discover, adapter (+ takes_context bridge), register, drain, run_worker

**Files:**

- Modify: `django_absurd/worker.py` (`discover_tasks`, `build_task_context`,
  `build_handler`, `register_tasks`, `drain_queue`, `run_worker`)
- Modify: `tests/tasks.py` (add worker test tasks)
- Modify: `tests/settings.py` (add `"other"` to the default alias's `QUEUES` — see
  Step 1)
- Test: `tests/test_worker.py`

**Interfaces:**

- Consumes: `open_worker_client`, `close_worker_client` (Task 1);
  `AbsurdBackend.{alias,queues}`; the SDK client (`claim_tasks`, `fetch_task_result`,
  `register_task`, `start_worker`).
- Produces (`worker.py`):

  - `discover_tasks(alias: str) -> list[Task]` — `autodiscover_modules("tasks")`, then
    for each `apps.get_app_configs()` look up `<app>.tasks` in `sys.modules` and collect
    `Task` instances whose `.backend == alias`, de-duplicated by `module_path`.
  - `build_task_context(task, ctx) -> TaskContext` — the bridge.
  - `build_handler(task) -> Callable[[Any, Any], Any]` — the adapter
    `handler(params, ctx)`.
  - `register_tasks(client, alias: str, queue: str) -> int` — registers
    `build_handler(task)` for every `discover_tasks(alias)` task under `queue`; returns
    the count; raises `ImproperlyConfigured` ("no tasks registered for backend
    '<alias>'; declare tasks in an installed app's tasks.py") when zero.
  - `drain_queue(client, *, claim_timeout=120, batch_size=None, worker_id=None) -> int`
    — burst loop:
    `tasks = client.claim_tasks(batch_size or 1, claim_timeout, worker_id or "worker")`;
    stop when `[]`; else, per task,
    `client._execute_task(t, claim_timeout)  # noqa: SLF001 -- SDK exposes no public counted dispatch; mirrors work_batch`.
    Return processed count. (This single, commented inline `# noqa: SLF001` is the ONLY
    lint ignore added — approved as the genuinely-unavoidable SDK-internal touch; the
    `_conn` access is avoided by `open_worker_client` returning the conn.)
  - `run_worker(backend, queue, *, burst=False, concurrency=1, claim_timeout=120, poll_interval=0.25, batch_size=None, worker_id=None) -> Absurd`
    — `open_worker_client`; `register_tasks(client, backend.alias, queue)`; log startup;
    then if `burst` → `drain_queue(...)` else `client.start_worker(...)`;
    `finally: close_worker_client(client)`. Returns the client.

- [ ] **Step 1: Write the test tasks + failing tests**

First, in `tests/settings.py`, add `"other"` to the default `TASKS` alias's `QUEUES`
(making it `["default", "other"]`). REQUIRED: `@task` validates `queue_name` against the
backend's declared queues at DECORATION time (`Task.__post_init__`), so the
`@task(queue_name="other")` task below would raise `InvalidTask` at import of
`tests.tasks` unless `"other"` is a declared queue. (The `routed` test still proves
backend-only discovery: `routed` is declared on `"other"` but spawned via
`using(queue_name="default")` onto the default queue, which the default worker runs.)

Append to `tests/tasks.py`:

```python
from django.contrib.auth.models import Group


@task
def make_group(name):
    Group.objects.create(name=name)
    return name


@task
def boom():
    msg = "boom"
    raise ValueError(msg)


@task(takes_context=True)
def report_attempt(context):
    return context.attempt


@task(queue_name="other")
def routed():
    Group.objects.create(name="routed")
    return "routed"
```

Append to `tests/test_worker.py`:

```python
from django.contrib.auth.models import Group

from django_absurd.worker import run_worker
from tests.tasks import make_group, report_attempt, routed


def burst(alias="default", queue="default"):
    # run_worker(burst=True) is the REAL run path: discover -> register ->
    # drain_queue (claim -> dispatch -> complete) -> exit. The worker claims, not the test.
    run_worker(get_absurd_backends()[alias], queue, burst=True)


def snapshot(task_id, alias="default", queue="default"):
    client, conn = open_worker_client(get_absurd_backends()[alias], queue)
    try:
        return client.fetch_task_result(task_id)
    finally:
        close_worker_client(client, conn)


def test_end_to_end_executes_and_records_result():
    call_command("absurd_sync_queues")
    result = make_group.enqueue("alpha")
    burst()
    assert Group.objects.filter(name="alpha").exists()
    snap = snapshot(result.id)
    assert snap.state == "completed"
    assert snap.result == "alpha"


def test_failing_task_records_failure():
    # Asserts the failure is RECORDED. Retry/reschedule is the SDK's behavior
    # (max_attempts), not re-asserted here — out of scope for SP3.
    from tests.tasks import boom

    call_command("absurd_sync_queues")
    result = boom.enqueue()
    burst()
    assert snapshot(result.id).state == "failed"


def test_takes_context_attempt_is_one_on_first_run():
    call_command("absurd_sync_queues")
    result = report_attempt.enqueue()
    burst()
    assert snapshot(result.id).result == 1


def test_using_queue_name_routes_to_worker_queue():
    call_command("absurd_sync_queues")
    routed.using(queue_name="default").enqueue()
    burst()
    assert Group.objects.filter(name="routed").exists()


def test_handler_logs_task_outcome(caplog):
    import logging

    call_command("absurd_sync_queues")
    make_group.enqueue("logged")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        burst()
    assert "tests.tasks.make_group" in caplog.text
    assert "completed" in caplog.text


def test_unregistered_name_defers_not_crashes():
    # A spawned name with no registered handler must DEFER (SDK reschedules),
    # not raise and not record a failure.
    call_command("absurd_sync_queues")
    be = get_absurd_backends()["default"]
    client, conn = open_worker_client(be, "default")
    try:
        spawn = client.spawn("not.a.real.task", {"args": [], "kwargs": {}}, queue="default")
    finally:
        close_worker_client(client, conn)
    burst()  # discovers/registers real tasks, drains; the unknown name defers, no raise
    assert snapshot(spawn["task_id"]).state != "failed"


def test_zero_tasks_for_alias_errors(settings):
    settings.TASKS = {"empty": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"DATABASE": "default"},
    }}
    call_command("absurd_sync_queues")
    with pytest.raises(ImproperlyConfigured, match="no tasks"):
        run_worker(get_absurd_backends()["empty"], "default", burst=True)
```

(`run_worker(..., burst=True)` is the deterministic driver — the real run path (discover
→ register → `drain_queue` → dispatch → complete → exit), so the WORKER claims tasks,
not the test. `tests.tasks` defines the worker test tasks on the default alias;
`test_zero_tasks_for_alias_errors` uses a SEPARATE alias `"empty"` with no tasks. The
observability test asserts the per-task log line via `caplog` on the `django_absurd`
logger, per the assert-emitted-text convention. Burst is sequential — concurrency is
exercised separately in Task 3's blocking smoke test.)

- [ ] **Step 2: Run to verify failures**

Run: `PGPORT=5433 uv run pytest tests/test_worker.py -v` Expected: FAIL —
`run_worker`/`register_tasks`/`discover_tasks` do not exist.

- [ ] **Step 3: Implement minimal (prose)**

- `discover_tasks(alias)`: call `autodiscover_modules("tasks")` (from
  `django.utils.module_loading`); iterate `apps.get_app_configs()`, get
  `sys.modules.get(f"{app.name}.tasks")`, and for a present module collect
  `vars(module).values()` that are `Task` instances (`from django.tasks import Task`)
  with `.backend == alias`. De-duplicate by `module_path` (a dict keyed on
  `module_path`). Return the list.
- `build_task_context(task, ctx)`: construct a Django `TaskResult`
  (`from django.tasks import TaskResult, TaskResultStatus`) with `task=task`,
  `id=ctx.task_id`, `status=TaskResultStatus.RUNNING`, `args=[]`, `kwargs={}`,
  `backend=task.backend`, `errors=[]`, `started_at=timezone.now()`, the other timestamps
  `None`, and `worker_ids=["absurd"] * ctx._task["attempt"]`; return
  `TaskContext(task_result=…)`. Do NOT pass `attempts=`.
- `build_handler(task)`: return `handler(params, ctx)` that calls
  `close_old_connections()` (from `django.db`), reads
  `args=params.get("args", [])`/`kwargs=params.get("kwargs", {})`, and calls
  `task.func(build_task_context(task, ctx), *args, **kwargs)` when `task.takes_context`
  else `task.func(*args, **kwargs)`; wrap in `try/finally` with a trailing
  `close_old_connections()`. Return value propagates (SDK completes); exceptions
  propagate (SDK fails). Add the per-task logging here (start/finish/exception with task
  name, `ctx.task_id`, attempt, outcome, duration) via a module
  `logging.getLogger("django_absurd")`.
- `register_tasks(client, alias, queue)`: `tasks = discover_tasks(alias)`; if empty
  raise `ImproperlyConfigured` ("no tasks registered for backend '<alias>'; declare
  tasks in an installed app's tasks.py"); for each
  `client.register_task(task.module_path, queue=queue)(build_handler(task))`; return
  `len(tasks)`.
- `drain_queue(client, *, claim_timeout=120, batch_size=None, worker_id=None)`: loop —
  `tasks = client.claim_tasks(batch_size or 1, claim_timeout, worker_id or "worker")`;
  `if not tasks: break`; else `for t in tasks: client._execute_task(t, claim_timeout)`;
  track and return the processed count. `client._execute_task` is the SDK-internal
  `work_batch` itself uses — the one encapsulated touch (place a short comment). This is
  the burst drain.
- `run_worker(backend, queue, *, burst=False, concurrency=1, claim_timeout=120, poll_interval=0.25, batch_size=None, worker_id=None)`:
  `client, conn = open_worker_client(backend, queue)`; then `try:`
  `count = register_tasks(client, backend.alias, queue)` (may raise
  `ImproperlyConfigured` for zero tasks — propagates), log a startup line (alias, queue,
  `backend.database`, burst/concurrency, count), then if `burst` →
  `drain_queue(client, claim_timeout=claim_timeout, batch_size=batch_size, worker_id=worker_id)`
  else (blocking) install SIGINT/SIGTERM handlers that call `client.stop_worker()`
  (restore the prior handlers afterward — `signal.signal` runs on the worker process's
  main thread; burst mode installs no handlers) and
  `client.start_worker(worker_id=worker_id, claim_timeout=claim_timeout, concurrency=concurrency, batch_size=batch_size, poll_interval=poll_interval)`;
  `finally: close_worker_client(client, conn)` (so the dedicated conn closes even when
  `register_tasks` raises). Return the client.

- [ ] **Step 4: Run to verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_worker.py -v` Expected: PASS. Run:
`PGPORT=5433 uv run pytest` · `uv run ruff check django_absurd tests` Expected: full
suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/worker.py tests/tasks.py tests/test_worker.py
git commit -m "feat: worker dispatch + takes_context bridge + drain_queue + run_worker (burst)"
```

---

### Task 3: `absurd_worker` management command (resolution, --burst, exit codes) + concurrency smoke test

**Files:**

- Create: `django_absurd/management/commands/absurd_worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**

- Consumes: `run_worker(backend, queue, *, burst=…, **tunables)`, `open_worker_client`,
  `close_worker_client`, `register_tasks` (Task 2); `get_absurd_backends`,
  `AbsurdBackend.{queues}` (SP1).
- Produces: `absurd_worker` management command — resolves alias
  (auto/require/error-list), requires + validates `--queue`, parses `--burst` + the five
  tunables, calls `run_worker`, maps `ImproperlyConfigured` → `CommandError` (exit
  non-zero), clean run → exit 0.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker.py`:

```python
import threading
import time

from django.core.management.base import CommandError

from django_absurd.worker import register_tasks  # used by the concurrency smoke test


def test_queue_is_required():
    with pytest.raises(CommandError):
        call_command("absurd_worker")


def test_unknown_queue_errors_listing_valid(settings):
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="nope")
    message = str(exc.value)
    assert "nope" in message          # the rejected queue
    assert "Valid queues" in message  # the allowlist is presented
    assert "default" in message       # ... and contains the real queue


def test_ambiguous_alias_requires_flag(settings):
    settings.TASKS = {
        "a": {"BACKEND": "django_absurd.backends.AbsurdBackend", "QUEUES": ["default"],
              "OPTIONS": {"DATABASE": "default"}},
        "b": {"BACKEND": "django_absurd.backends.AbsurdBackend", "QUEUES": ["default"],
              "OPTIONS": {"DATABASE": "default"}},
    }
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="default")
    message = str(exc.value)
    assert "a" in message
    assert "b" in message


def test_command_parses_all_flags_with_defaults():
    from django.core.management import load_command_class

    cmd = load_command_class("django_absurd", "absurd_worker")
    parser = cmd.create_parser("manage.py", "absurd_worker")
    opts = vars(parser.parse_args(["--queue", "default"]))
    assert opts["queue"] == "default"
    assert opts["alias"] is None
    assert opts["burst"] is False
    assert opts["concurrency"] == 1
    assert opts["claim_timeout"] == 120
    assert opts["poll_interval"] == 0.25
    assert opts["batch_size"] is None
    assert opts["worker_id"] is None  # passed through; SDK synthesizes host:pid


def test_command_burst_runs_task_end_to_end():
    # The full command path: resolve -> run_worker -> drain -> dispatch -> complete -> exit.
    from tests.tasks import make_group

    call_command("absurd_sync_queues")
    result = make_group.enqueue("via-command")
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="via-command").exists()
    assert snapshot(result.id).state == "completed"


def test_command_maps_improperly_configured_to_commanderror(settings):
    # Declare a queue that is never synced -> run_worker raises ImproperlyConfigured
    # (provisioning check) -> command surfaces it as CommandError.
    call_command("absurd_sync_queues")
    settings.TASKS = {"default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default", "unsynced"],
        "OPTIONS": {"DATABASE": "default"},
    }}
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="unsynced", burst=True)
    assert "unsynced" in str(exc.value)


def test_start_worker_drains_concurrently():
    from tests.tasks import make_group

    call_command("absurd_sync_queues")
    for i in range(5):
        make_group.enqueue(f"g{i}")

    be = get_absurd_backends()["default"]
    client, conn = open_worker_client(be, "default")
    register_tasks(client, "default", "default")
    worker = threading.Thread(
        target=lambda: client.start_worker(concurrency=3, poll_interval=0.05),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 20
    while Group.objects.filter(name__startswith="g").count() < 5:
        if time.monotonic() > deadline:
            client.stop_worker()
            worker.join(5)
            raise AssertionError("worker did not drain in time")
        time.sleep(0.1)
    client.stop_worker()
    worker.join(5)
    close_worker_client(client, conn)
    assert Group.objects.filter(name__startswith="g").count() == 5
```

(`test_command_burst_runs_task_end_to_end` proves the whole command wiring via
`call_command(..., burst=True)`. The concurrency test drives `client.start_worker`
directly in a thread — burst is sequential, so concurrency is only exercisable via the
blocking loop — and polls a real DB condition (`Group` count == 5) with a timeout BEFORE
`stop_worker()`; never sleep-then-stop. `stop_worker()` from the main thread clears the
SDK run flag; the loop checks it each iteration and drains in-flight futures, so the
thread exits. In-process SIGINT/SIGTERM delivery is NOT integration-tested — too flaky;
the signal handler is a thin `stop_worker` wrapper installed in `run_worker`'s blocking
branch, exercised manually here via `stop_worker`. `register_tasks` is imported at the
top of the file from Task 2.)

- [ ] **Step 2: Run to verify failures**

Run:
`PGPORT=5433 uv run pytest tests/test_worker.py -k "queue_is_required or unknown_queue or ambiguous_alias or parses or burst or maps or drains" -v`
Expected: FAIL — the `absurd_worker` command does not exist.

- [ ] **Step 3: Implement minimal (prose)**

- Create `django_absurd/management/commands/absurd_worker.py`. `add_arguments` declares
  `--queue` (`required=True`), `--alias` (default `None`), `--burst`
  (`action="store_true"` — defaults to False), `--concurrency` (int, default 1),
  `--claim-timeout` (int, default 120), `--poll-interval` (float, default 0.25),
  `--batch-size` (int, default None), `--worker-id` (default None) — `--burst` + the
  COMPLETE `start_worker` signature; pass `--worker-id` through as-is (`None` → the SDK
  synthesizes `<host>:<pid>`; do NOT reimplement that format).
- In `handle`: resolve the alias from `get_absurd_backends()` — if `--alias` given use
  it (error via `CommandError` if not an Absurd alias); else if exactly one Absurd
  backend use it; else `CommandError` listing the Absurd aliases. Validate
  `--queue in backend.queues` else raise `CommandError` whose message uses the exact
  form
  **`"Queue '<q>' is not declared for backend '<alias>'. Valid queues: <comma-joined sorted(backend.queues)>"`**
  (the test asserts the substrings `"nope"`, `"Valid queues"`, `"default"`). Then call
  `run_worker(backend, queue, burst=options["burst"], concurrency=…, claim_timeout=…, poll_interval=…, batch_size=…, worker_id=…)`,
  wrapping it so an `ImproperlyConfigured` (unprovisioned schema/queue, zero tasks) is
  re-raised as `CommandError` (Django exits non-zero); a clean return → exit 0.
- (Signals live in `run_worker`'s blocking branch from Task 2; the command does not
  install them.)

- [ ] **Step 4: Run to verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_worker.py -v` Expected: PASS. Run:
`PGPORT=5433 uv run pytest` · `PGPORT=5433 uv run pytest tests/multidb` ·
`uv run ruff check django_absurd tests` · `PGPORT=5433 uvx --with tox-uv tox` Expected:
both suites green; ruff clean; full 6-env matrix green.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/management/commands/absurd_worker.py tests/test_worker.py
git commit -m "feat: absurd_worker command (alias/queue resolution, --burst, exit codes)"
```

---

## Self-Review

**Spec coverage:** connection (dedicated autocommit, spawned from Django config,
psycopg3 assert, provisioning check) + shared jsonb loader → T1; discovery
(backend-only, tasks.py, zero→error) + adapter + takes_context bridge
(worker_ids-length, 1-based) + at-least-once (ORM commits in handler) + observability
logging + `drain_queue` (burst) + `run_worker` (burst/blocking, signals in blocking
branch) → T2; command surface (`--queue` required+validated, `--alias` auto/ambiguous,
`--burst` + five tunables, `--worker-id` None) + ImproperlyConfigured→CommandError
mapping + burst e2e + concurrency smoke (blocking) → T3. Out-of-scope items (async,
get_result, connection-per-thread, ALWAYS_EAGER, idempotency) correctly absent.

**Type consistency:** `open_worker_client(backend, queue) -> Absurd`,
`close_worker_client(client)`, `discover_tasks(alias) -> list[Task]`,
`build_handler(task)`, `build_task_context(task, ctx)`,
`register_tasks(client, alias, queue) -> int`,
`run_worker(backend, queue, *, …) -> Absurd`, and `register_jsonb_loader(raw_conn)` are
used identically across T1–T3 and match SP1's `AbsurdBackend.{alias,database,queues}` +
SP2's client.

**Placeholder scan:** no TBD/TODO; test steps carry full assertion code; implementation
steps are prose (no production code) per the no-coding-ahead rule. Provisioning-probe
exception set reuses SP2's verified
`InvalidSchemaName`/`UndefinedTable`/`UndefinedFunction`.

**Connection lifecycle (opus-review C1, empirically confirmed by spike):**
`Absurd(conn)` does NOT own the connection (`_owned_conn is False`), so `client.close()`
is a no-op on the socket. `open_worker_client` returns `(client, conn)`; every close
goes through `close_worker_client(client, conn)` → `client.close()` + `conn.close()`. NO
`client._conn` access anywhere (returning the conn avoids it).

**Lint (no ruff ignores except one approved inline noqa):** `boom` uses
`msg = …; raise ValueError(msg)` (EM101); ambiguous-alias / e2e tests use separate
asserts (PT018); the `worker` thread var has no leading underscore. The ONLY ignore is a
single commented inline `# noqa: SLF001` on `drain_queue`'s `client._execute_task(...)`
call (sonnet-review C2; user-approved) — genuinely unavoidable (the SDK exposes no
public counted dispatch; `work_batch` itself uses `_execute_task`). `client._conn` is
NOT accessed (eliminated via the tuple return). `client._conn`/`client._execute_task` in
TEST code is covered by the pre-existing `tests/**` `SLF001` ignore.

**Sonnet-review fixes folded in:** C1 — `tests/settings.py` `QUEUES` gains `"other"` (so
`@task(queue_name="other")` validates at decoration); C2 — the single approved inline
noqa + tuple-return; C3 — `register_tasks` imported in T3's test block.

**Burst-mode restructure (per the user + plan-review pivot):** behavioral tests drive
the real run path — `run_worker(burst=True)` in T2,
`call_command("absurd_worker", …, burst=True)` in T3 — so the WORKER claims tasks, not
the test (no manual `spawn`+`claim`+`work_batch` simulation). `--burst` is a real
RQ-style feature (drain-then-exit) doubling as the deterministic test entry point. Burst
is sequential; concurrency uses the one blocking `start_worker` smoke test.

**Added coverage (opus review):** observability (`test_handler_logs_task_outcome` via
`caplog`), unregistered-name-defers (`test_unregistered_name_defers_not_crashes`), and
tunable/`--worker-id` passthrough (`test_command_parses_all_tunables_with_defaults`).
Retry-count and in-process signal/exit-code integration are consciously de-scoped
(SDK-owned / flaky in-process) with inline notes.

**Testing level:** every behavioral test drives `run_worker`/`work_batch`/`call_command`
and asserts DB rows (`Group`), Absurd result snapshots (`fetch_task_result`), emitted
log/`CommandError`/`ImproperlyConfigured` text, or the poll-drained Group count — no
internal/unit assertions on discovery/adapter internals (the one parser test asserts the
command's public arg surface).
