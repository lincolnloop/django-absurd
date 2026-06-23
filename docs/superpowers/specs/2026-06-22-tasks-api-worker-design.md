# django-absurd — Spec: Tasks-API consume side / worker (SP3)

Date: 2026-06-22 Status: approved-for-planning

Third sub-project of the Django Tasks integration. SP1 built the config (`AbsurdBackend`
in `TASKS`); SP2 the produce side (`enqueue` → `spawn`). SP3 builds the **consume
side**: `manage.py absurd_worker`, a long-running process that claims tasks from one
queue, resolves each to its `@task` function, executes it, and (via the SDK) records
completion/failure back to Absurd. Sync only.

## Scope

- Sync worker. Async worker (`AsyncAbsurd` + flipping `supports_async_task`) stays
  deferred — the project has no async tasks (`supports_async_task=False`).
- No Django-side result writeback — `get_result` is deferred
  (`supports_get_result= False`); the SDK persists results to Absurd. The worker doesn't
  touch Django `TaskResult` storage.
- **Delivery guarantee: at-least-once.** A task's own ORM writes commit per Django's
  normal rules, independently of Absurd marking the run complete — there is NO atomicity
  between "handler ORM write" and "Absurd records completion." If the process dies after
  the handler commits but before the SDK's `complete_run`, Absurd retries and the
  handler runs again. Task authors must treat handlers as potentially re-run
  (idempotency keys are deferred).
- **psycopg3/Postgres is required only for Absurd's bookkeeping**, not for what tasks
  do. The worker's dedicated connection (claim/complete/fail + queue tables) must be
  psycopg3/Postgres (the `validate_backend` assertion; Absurd is Postgres-native). A
  task's own `@task` body is ordinary Python using Django's ORM however it likes —
  including a DIFFERENT Django database alias (`Model.objects.using("sqlite")`, a
  router, etc.) on thread-local Django connections, entirely separate from the Absurd
  connection. Caveat: there is no cross-DB atomicity between a task's write to another
  DB and Absurd's `complete_run` (compounds the at-least-once guarantee above).

## Command

```
manage.py absurd_worker --queue <name> [--alias <key>] [--burst]
    [--concurrency N] [--claim-timeout S] [--poll-interval S]
    [--batch-size N] [--worker-id ID]
```

- **`--alias`** — the `TASKS` dict key (NOT the `BACKEND` import path). Auto-resolves
  when exactly one `AbsurdBackend` is configured across `TASKS` (via
  `get_absurd_backends()`); if more than one, required, and the command errors listing
  the Absurd aliases.
- **`--queue`** — REQUIRED, no default. Validated against the resolved backend's
  declared `QUEUES` (the `self.queues` allowlist); an unknown queue errors listing the
  valid ones. A worker serves exactly ONE queue (the SDK claims from a single queue).
  Run one worker per queue.
- **`--burst`** — process the available backlog then EXIT (0), instead of blocking
  forever. RQ-style "burst mode": a real production feature (drain a backlog, cron/CI
  one-shot runs) AND the deterministic entry point the tests drive. Sequential (does not
  use the threadpool); `--concurrency` is ignored in burst mode. Without `--burst` the
  worker runs the blocking `start_worker` loop until SIGINT/SIGTERM.
- The five `start_worker` tunables are exposed as flags — the COMPLETE `start_worker`
  signature, nothing omitted — with the SDK defaults: `--concurrency` (1),
  `--claim-timeout` (120), `--poll-interval` (0.25), `--batch-size` (None),
  `--worker-id` (**None** — passed through so the SDK synthesizes its own
  `<host>:<pid>`; the command does NOT reimplement that format).

## Run modes (blocking vs burst)

`run_worker(backend, queue, *, burst=False, …)` opens the client, registers handlers
(below), logs startup, then:

- **Blocking (default):**
  `client.start_worker(concurrency, claim_timeout, poll_interval, batch_size, worker_id)`
  — runs until `stop_worker()` (signal). Threadpool when `concurrency>1` (see
  Concurrency).
- **Burst (`--burst`):** `drain_queue(client, claim_timeout, batch_size, worker_id)` —
  loop: `tasks = client.claim_tasks(batch_size, …)`; stop when it returns `[]`; else
  dispatch each via the SDK's per-task execution (the same `_execute_task` path
  `work_batch` uses — one encapsulated SDK-internal touch) so completion/failure are
  recorded identically. Returns the processed count; the command exits 0. Sequential.

The SDK exposes no public "pending count", so the claim-until-empty loop is the reliable
drain detector (`claim_tasks` returns `[]` when empty; `work_batch` itself is just
`claim_tasks` + per-task execute, with no count returned).

## Connection

The worker opens a **dedicated** psycopg connection, NOT Django's shared request
connection (a standalone worker has no request transaction to ride, and the SDK's
claim/complete/fail must commit independently). It is spawned from Django's own config
so no separate connection settings are introduced:

- `params = connections[backend.database].get_connection_params()` — returns
  psycopg-ready kwargs (`dbname`/`host`/`port`/`user`/`password`, plus `cursor_factory`,
  `context`, `prepare_threshold`). Django passes this same dict straight into
  `psycopg.connect()` and every key is a real `psycopg.connect` kwarg, so **no
  stripping/translation is required**. (`cursor_factory` is Django's `Cursor` subclass;
  the SDK makes its own cursors via `conn.cursor(row_factory=dict_row)` which works fine
  with it — we intentionally let it ride.)
- Open the connection with **`autocommit=True`** explicitly (`get_connection_params()`
  does NOT include autocommit — Django sets it separately). Autocommit is a
  **correctness precondition**, not a convenience — see Concurrency.
- Build `Absurd(conn, queue_name=queue)`.
- Assert the psycopg3 backend first (reuse `validate_backend(backend.database)`); a
  non-psycopg3 alias raises `ImproperlyConfigured`.
- Register the jsonb loader the enqueue client uses (so `claim_tasks` returns dicts, not
  raw strings — SP2's `set_json_loads`). **Factor this into a shared helper** so the
  worker client and `get_absurd_client` can't diverge (M-4).
- Close the connection on worker exit.
- **Provisioning check at startup (I-4):** before entering the loop, validate the absurd
  schema + the target queue exist on the worker's DB; if not, raise the SP2-style
  `ImproperlyConfigured` naming the queue and pointing at `manage.py absurd_sync_queues`
  / `manage.py migrate` — rather than letting the first `claim_tasks` blow up with a raw
  psycopg error.

A new function owns this — e.g. `open_worker_client(backend, queue) -> Absurd` —
distinct from `get_absurd_client` (which intentionally reuses Django's connection for
enqueue).

## Discovery + registration

Django keeps no global task registry and `autodiscover_modules` returns nothing, so the
worker imports task modules then re-walks them:

1. `autodiscover_modules("tasks")` (from `django.utils.module_loading`) imports every
   installed app's `tasks.py` (side effect: `@task` creates `Task` objects). **Contract:
   tasks must be declared in an installed app's `<app>/tasks.py`** — tasks defined
   elsewhere are not discovered (documented limitation, Django/Celery convention).
2. Concrete enumeration: iterate `apps.get_app_configs()`, look up `<app>.tasks` in
   `sys.modules` (present iff the app has a `tasks` module), and collect `Task`
   instances among the module's members.
3. **Filter on backend alias ONLY** (`task.backend == alias`) — NOT on queue. Register
   every such task under the worker's `--queue`:
   `client.register_task(task.module_path, queue=queue)(build_handler(task, alias))`.

   Rationale: queue routing is decided by what the producer spawned, not by the task's
   declared `queue_name`. A task spawned via `task.using(queue_name="x")` keeps the base
   task's `module_path` but lands on queue `x`; filtering registration on the base
   task's `queue_name` would exclude it and it would defer forever. Registering every
   alias task under the worker's queue means any name that lands on this queue resolves;
   over-registration is harmless (a name only runs here if something actually spawned it
   onto this queue, and unknown names defer rather than fail). Registering with
   `queue=queue` (the worker's queue) also avoids the SDK's "queue mismatch" hard-fail
   (registration queue must equal the worker client's `queue_name`). Duplicate
   enumeration (a `Task` imported into several `tasks.py`) is benign — `register_task`
   is last-wins on `_registry[name]`.

4. **Zero tasks → error at startup (I-3):** if no `Task` for the alias is discovered,
   raise a clear error ("no tasks registered for backend '<alias>'; tasks must be
   declared in an installed app's tasks.py") rather than idling as a silent no-op.

`task.module_path` is the dotted path the producer spawned under (SP2), so registered
names match incoming task names. Names not registered simply never run here — the SDK
defers unknown names rather than failing.

## Adapter (`build_handler`)

Returns `handler(params, ctx)` for the SDK:

```
def handler(params, ctx):
    close_old_connections()
    try:
        args = params.get("args", [])
        kwargs = params.get("kwargs", {})
        if task.takes_context:
            context = build_task_context(task, ctx)
            return task.func(context, *args, **kwargs)
        return task.func(*args, **kwargs)
    finally:
        close_old_connections()
```

- `close_old_connections()` (Django) before and after: a worker never fires
  `request_finished`, so per-thread Django ORM connections must be cycled to stay fresh
  and not leak across tasks. Scope is the pool thread / per task — correct for
  thread-local connections.
- Return value → SDK calls `complete_run`. A raised exception propagates → SDK calls
  `fail_run` → Absurd reschedules per the task's `max_attempts`. The adapter does NOT
  catch task exceptions.
- For `takes_context` tasks, `context` is the reserved first positional argument
  (matches Django's immediate/dummy backend convention).

### `takes_context` bridge (`build_task_context`)

Django's `TaskContext` is `{task_result: TaskResult}`; `TaskContext.attempt` returns
`task_result.attempts`, and **`TaskResult.attempts` is a read-only property
`== len(self.worker_ids)`** (NOT an init field — passing `attempts=` raises
`TypeError`). The Absurd handler ctx exposes `ctx.task_id` (public) and `ctx._task` (the
claimed row, carrying `attempt`, which is **1-based** — `1` on first run, matching
Django's expectation).

Build the Django `TaskResult` so the property reflects the attempt:

- `task=task`, `id=ctx.task_id`, `status=TaskResultStatus.RUNNING`, `args=list(args)`,
  `kwargs=dict(kwargs)`, `backend=alias`, `errors=[]`, `started_at=timezone.now()`,
  `enqueued_at`/`finished_at`/`last_attempted_at=None`, and
  **`worker_ids=[worker_id] * ctx._task["attempt"]`** so that
  `task_result.attempts == ctx._task["attempt"]` (== `context.attempt`). Do NOT pass
  `attempts=`.
- Wrap in `TaskContext(task_result=…)`.

`ctx._task["attempt"]` reaches into one SDK internal (the public ctx has no `attempt`
accessor) — an accepted coupling, noted so a future SDK accessor can replace it.

## Concurrency

`client.start_worker(concurrency=N, …)`. With `N>1` the SDK runs task bodies in a thread
pool, all sharing the one dedicated connection for bookkeeping. Safety rests on TWO
facts, both required:

1. **psycopg3 `threadsafety == 2`** — a connection is thread-safe; the SDK's
   `claim_tasks` (main thread) and `complete_run`/`fail_run` (pool threads) on the
   shared `self._conn` serialize correctly.
2. **autocommit (precondition).** Because those interleaved bookkeeping statements share
   one connection, without autocommit they would share one TRANSACTION → visibility/
   locking hazards. Autocommit makes each bookkeeping statement its own transaction.
   This is why the worker connection MUST be autocommit for `concurrency>1` to be
   correct.

Handler ORM uses **thread-local** Django connections (separate from the SDK connection),
genuinely parallel, cycled by the adapter's `close_old_connections()`. Only the fast
bookkeeping SQL serializes; task bodies scale. Scale beyond one process's bookkeeping by
running more worker processes. A connection-per-thread custom loop is a logged future
optimization, to revisit only with profiling evidence.

## Observability + exit codes (M-3)

- **Per-task logging:** log each task's start and outcome (task name, task_id, attempt,
  success/failure, duration) via the stdlib `logging` (a `django_absurd` logger), so an
  operator can see what ran. Wire into the adapter (start/finish/exception).
- **Startup logging:** log the resolved alias, queue, DB, concurrency, and the count +
  names of registered tasks.
- **Exit codes:** non-zero on a fatal startup error (bad alias, unprovisioned
  schema/queue, zero tasks); `0` on a clean SIGTERM/SIGINT drain.

## Shutdown

The command installs SIGINT/SIGTERM handlers that call `client.stop_worker()` (clears
the SDK's run flag so the loop drains in-flight work and returns), then closes the
dedicated connection and exits `0`. No forced/abrupt kill in the normal path; in-flight
tasks finish before the loop returns.

## Files

- Create: `django_absurd/worker.py` — `open_worker_client(backend, queue) -> Absurd`;
  `close_worker_client(client)`; `discover_tasks(alias) -> list[Task]`;
  `build_handler(task)` (+ `build_task_context`);
  `register_tasks(client, alias, queue) -> int`; `drain_queue(client, …) -> int`
  (burst); `run_worker(backend, queue, *, burst=False, **opts)` orchestration (validate
  → discover → register → start_worker|drain → cleanup); the shared jsonb-loader helper
  (extracted from `get_absurd_client`).
- Modify: `django_absurd/queues.py` — extract the jsonb-loader registration into the
  shared helper `open_worker_client` reuses.
- Create: `django_absurd/management/commands/absurd_worker.py` — arg parsing,
  alias/queue resolution + validation, signal handlers, exit codes, calls `run_worker`.
- Test: `tests/tasks.py` (extend with worker test tasks), `tests/test_worker.py`.

## Testing (pytest, function-based, real Postgres via compose; single-DB suite)

`@pytest.mark.django_db(transaction=True)` (spawn/claim commit + queue DDL).
**Behavioral tests drive the REAL command** —
`call_command("absurd_worker", queue=…, burst=True)` — which runs the full path
(alias/queue resolution → `open_worker_client` → discover → register → `drain_queue` →
dispatch → complete → exit). Burst makes this deterministic (no threads, no manual claim
poking). Two tests stay lower-level by necessity: one white-box `open_worker_client`
connection test, and the blocking-`start_worker` concurrency smoke test.

Cases:

- **End-to-end via command:** sync the queue, enqueue a task that writes a `Group` row,
  `call_command("absurd_worker", queue="default", burst=True)`, assert the row exists
  AND the Absurd result snapshot is `completed` with the return value. (Exercises
  ORM-in-handler through the adapter's `close_old_connections()` too.)
- **Failure → recorded:** a task that raises → snapshot `failed`. (Retry/reschedule is
  SDK-owned — not re-asserted; out of scope.)
- **takes_context bridge:** a `@task(takes_context=True)` returns `context.attempt`;
  assert the recorded result is `1` on first run (locks in the worker_ids-length /
  1-based fix).
- **using(queue_name=) routing:** a task declared on queue A, spawned via
  `using(queue_name="default")`, IS executed by the default burst worker (locks in the
  backend-only discovery filter).
- **Unregistered name defers, not crashes:** a spawned name with no registered handler →
  burst run does not raise and the snapshot is not `failed` (SDK defers it).
- **Observability:** `caplog` on the `django_absurd` logger shows the per-task line
  (name/outcome) after a burst run.
- **Zero tasks → startup error:** an alias with no discoverable tasks → the command
  raises `CommandError` ("no tasks registered…").
- **Unprovisioned schema/queue → clear error:** drop the absurd schema (the
  `test_checks`/`test_enqueue` pattern, restore via migrate in `finally`) → the command
  raises `CommandError` (mapped from the SP2-style `ImproperlyConfigured`, naming the
  queue
  - `absurd_sync_queues`/`migrate`).
- **`--queue` required + validated:** omitting `--queue` → `CommandError`; an undeclared
  queue → `CommandError` presenting the valid-queue allowlist.
- **`--alias` resolution:** single Absurd backend → auto-resolved; two distinct Absurd
  aliases → omitting `--alias` → `CommandError` listing them.
- **Flag surface:** the command's parser exposes `--burst` + the five tunables with SDK
  defaults and `--worker-id` defaulting to `None` (passthrough).
- **Connection (white-box):** `open_worker_client` returns a client on a DEDICATED
  autocommit connection (not Django's shared request connection); non-psycopg3 alias →
  `ImproperlyConfigured`.
- **Concurrency (the one blocking-loop smoke test):** enqueue N tasks, run
  `start_worker(concurrency>1)` in a background thread; **poll a real DB condition**
  (the `Group` row count == N) with a timeout, THEN `stop_worker()` and join. NOT
  sleep-then-stop — the poll-until-drained gate is required. (Burst is sequential, so
  concurrency is only exercisable via the blocking loop.)

## Out of scope (future sub-projects, logged in memory `deferred-tasks-api-followups`)

- Async worker (`AsyncAbsurd`) + native async enqueue + `supports_async_task=True`.
- Result retrieval (`get_result`, read-model), `supports_get_result=True`.
- Connection-per-thread custom worker loop (bookkeeping-parallelism optimization).
- `ALWAYS_EAGER` (Celery-like) inline auto-execute that still round-trips through
  `spawn`
  - the Absurd tables (reuses this worker's claim+dispatch path).
- Idempotency keys (the at-least-once delivery mitigation), `run_after`/defer, priority.
