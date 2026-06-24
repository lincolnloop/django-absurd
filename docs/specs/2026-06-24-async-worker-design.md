# django-absurd — Spec: native-async worker (SP7)

Date: 2026-06-24 Status: approved-for-planning

Make `absurd_worker` a native-asyncio worker so it runs BOTH sync and `async def` tasks,
and flip `AbsurdBackend.supports_async_task = True`. Today the worker is
sync/thread-based (`Absurd` + `psycopg.Connection` + the SDK's threaded `start_worker`)
and calls `task.func(*args, **kwargs)` directly — a coroutine task func would return an
un-awaited coroutine, so `supports_async_task=False` rejects async tasks at enqueue.

## Why / decisions (from brainstorm)

- django-absurd is a LIBRARY; developers decide sync vs async, incl. high-I/O async
  tasks
  - async ORM. So the worker must run async tasks NATIVELY (a true event loop), not via
    an `async_to_sync` bridge on a thread worker.
- **REPLACE, not coexist.** A native-async worker is a strict SUPERSET: `async def`
  tasks are awaited on the loop (true concurrency); `def` tasks run in a thread pool via
  `loop.run_in_executor` — byte-for-byte the thread behavior today's worker gives them
  (`close_old_connections` per task, sync ORM fine). One `absurd_worker`, one
  implementation, one connection model. No capability lost.
- **Invocation unchanged.** Same command/flags
  (`absurd_worker --queue … [--concurrency N] [--burst] …`); the async loop is internal.
  Sync developers write plain `def` tasks + sync ORM as before — they never see a loop.
- **Worker-only scope.** Native async `aenqueue` (produce side via `AsyncAbsurd` /
  `AsyncConnection`) is OUT — the inherited
  `BaseTaskBackend.aenqueue = sync_to_async(self.enqueue)` stays (a fast spawn;
  adequate). Deferred follow-up.

## SDK facts (verified)

`AsyncAbsurd(conn_or_url: AsyncConnection | str, queue_name="default", default_max_attempts=5, hooks=None)`.
Coroutines: `claim_tasks`, `_execute_task`,
`start_worker(worker_id, claim_timeout=120, concurrency=1, batch_size=None, poll_interval=0.25)`.
`stop_worker(self) -> None` is SYNC (sets a stop flag) → safe to call from
`loop.add_signal_handler`. `_execute_task` AWAITS the registry entry's `handler` (an
`AsyncTaskHandler`), so our handler must be `async`.
`psycopg.AsyncConnection.connect(**params, autocommit=True)` mirrors the sync dedicated
connection — BUT Django's `get_connection_params()` includes
`cursor_factory=<sync Django Cursor>`, which is FATAL for `AsyncConnection.connect` (the
connection's `.cursor()` returns a sync cursor → the SDK's `await cursor.execute(...)`
raises on the FIRST call, `list_queues()`). Must `params.pop("cursor_factory", None)`
before connecting (live-verified: dropping that one key yields an `AsyncCursor` and
async execute works; other params — `context`/`client_encoding`/`prepare_threshold` —
are accepted). `register_jsonb_loader(context)` already accepts any `AdaptContext` incl.
an `AsyncConnection`. Verified live: `AsyncAbsurd` reads a swapped `_registry` via
`.get(name)` and `await`s `registration["handler"]` — same entry-dict shape as sync; the
ctx is an `AsyncTaskContext` with `ctx.task_id` + `ctx._task["attempt"]`, so
`read_sdk_attempt`/`build_task_context` reuse unchanged.

## Architecture

`absurd_worker` command (CLI unchanged) →
`run_worker(backend, queue, *, burst, options)` stays a SYNC entry that just does
`asyncio.run(arun_worker(...))` — the command and `run_worker`'s signature don't change.
Everything below runs on the loop.

- **`aworker_client(backend, queue)`** — async ctx-mgr mirroring the sync
  `worker_client`:
  `params = connections[backend.database].get_connection_params(); params.pop("cursor_factory", None); conn = await psycopg.AsyncConnection.connect(**params, autocommit=True)`
  (DEDICATED — NOT Django's registered conn, so `close_old_connections()` never closes
  it under the SDK; `cursor_factory` dropped per C1 above),
  `register_jsonb_loader(conn)`, `client = AsyncAbsurd(conn, queue_name=queue)`, install
  `client._registry = LazyTaskRegistry(queue)` (one SLF001), provisioning check via
  `await client.list_queues()` → `ImproperlyConfigured` on absent schema / unprovisioned
  queue (same messages as today), `finally: await conn.close()`.
- **`LazyTaskRegistry`** — same resolution logic (cache miss → `import_string(name)`,
  `ImportError`/non-`Task` → `default` so the SDK defers, else cache the entry dict
  `{name, queue, default_max_attempts:None, default_cancellation:None, handler}`). ONLY
  the built handler changes: now an `async` handler.
- **async `build_handler(task)`** — `async def handler(params, ctx)`:
  - `args`/`kwargs` from `params`; `attempt = read_sdk_attempt(ctx)`;
    start/timing/logging identical to today.
  - **Dispatch by func kind:** `if inspect.iscoroutinefunction(task.func):` await it on
    the loop — `await task.func(ctx_, *args, **kwargs)` (with-context) or
    `await task.func(*args, **kwargs)`. **Else (sync):**
    `await loop.run_in_executor(executor, call_sync)` where `call_sync` does
    `close_old_connections()` → `task.func([ctx_,] *args, **kwargs)` →
    `close_old_connections()` (so sync ORM connections are fresh per task in the
    executor thread, exactly as today's threaded handler).
  - `takes_context`: `build_task_context(task, ctx, args, kwargs)` REUSED unchanged
    (data-only); the `ctx_` is passed first positional to either path.
  - error/`logger.exception`/duration logging + re-raise: unchanged (so the SDK records
    the failure and retries).
- **burst** — async `drain_queue(client, …)`: loop
  `claimed = await client.claim_tasks( batch_size or 1, claim_timeout, worker_id or "worker")`
  until empty, `await client._execute_task(t_, claim_timeout)` each (one SLF001, mirrors
  today).
- **blocking** — async `run_blocking_worker(client, options)`: install
  `loop.add_signal_handler(SIGINT, client.stop_worker)` + SIGTERM (stop_worker is sync),
  `await client.start_worker(worker_id, claim_timeout, concurrency, batch_size, poll_interval)`,
  remove the signal handlers in `finally`.
- **Executor / concurrency:** one
  `concurrent.futures.ThreadPoolExecutor(max_workers= options.concurrency)` created in
  `arun_worker`, used by sync-task handlers via `run_in_executor`. `--concurrency N`
  drives BOTH the SDK's loop concurrency (`start_worker(concurrency=N)`) AND the
  executor pool size (one knob). Executor shut down on exit.
- `read_sdk_attempt`, `build_task_context`, `WorkerOptions`, the command's alias/queue
  resolution + `--burst`/tunables + `ImproperlyConfigured→CommandError` mapping: all
  REUSED unchanged.
- `AbsurdBackend.supports_async_task = True`.

## Dropped (replaced by async equivalents)

Sync `worker_client`, sync `drain_queue`, sync `run_blocking_worker`, sync
`build_handler`, the sync-handler-building branch of `LazyTaskRegistry`, the
`signal.signal()` thread approach (→ `loop.add_signal_handler`; KEEP `import signal` —
the `SIGINT`/`SIGTERM` constants are still needed), `from absurd_sdk import Absurd` (→
`AsyncAbsurd`). Git history preserves them. NO capability lost (async worker is a
superset). Note: `loop.add_signal_handler` raises on Windows / off the main thread —
guard or document (out-of-scope to fully solve; worker runs on the main thread under
`asyncio.run`).

## Connection model

Worker claim/bookkeeping on a dedicated `AsyncConnection` (built from Django's DB
params, autocommit). Sync tasks run in executor threads using Django's thread-local SYNC
connections (`close_old_connections` around each) — unchanged from today. Async tasks
doing async ORM run on the loop (Django async ORM works in an async context; sync ORM
inside an `async def` raises `SynchronousOnlyOperation` — the task author's concern,
standard Django).

## Testing (pytest, function-based, real Postgres; behavior-first)

**Existing behavioral tests stay GREEN, unchanged** — they drive
`call_command( "absurd_worker", queue=…, burst=True)` and assert observable state
(`Group` rows, result snapshots). The worker being asyncio internally doesn't change the
observable outcome or the CLI.

**Internal-API-touching tests adapt** (behavior unchanged, internals went async):

- `get_task_result(...)` helper currently fetches via the sync `worker_client` (which
  registers the jsonb loader on its DEDICATED conn — that's why `snap.result` decodes).
  Repoint it at a read that ALSO registers the loader on a DEDICATED conn — do NOT use
  `get_absurd_client(...).fetch_task_result(...)`: `build_absurd_client` does NOT
  register the loader (so `result` comes back as a raw JSON STRING, breaking 4
  `snap.result ==` assertions — C2), and registering the loader on `get_absurd_client`'s
  SHARED Django conn would re-introduce the SP6 JSONField-poison. So the helper opens a
  dedicated sync `psycopg.connect(**params)` + `register_jsonb_loader(conn)` +
  `client.fetch_task_result`, OR drives the async `aworker_client` via `asyncio.run` (it
  already registers on its dedicated conn). (`claim_one` is unaffected — it registers
  the loader itself.)
- `worker_client` provisioning/connection tests (dedicated-conn, unprovisioned,
  absent-schema) → rewrite against async `aworker_client` (await it in an
  `asyncio.run`), or fold the unprovisioned/absent-schema cases into the existing
  command-level `ImproperlyConfigured→CommandError` tests.
- **`test_unregistered_name_defers_not_crashes` (I1)** uses
  `with worker_client(...) as client: client.spawn(...)`. Rewrite the spawn via
  `get_absurd_client("default").spawn( "not.a.real.task", ...)` (sync, no worker
  internals; spawn reads no jsonb), keep the burst run + "not failed (deferred)"
  assertion.
- **the threaded concurrency test (I2)** `test_start_worker_drains_concurrently` uses a
  `threading.Thread` running sync `start_worker` + `stop_worker`. The async
  `start_worker` is a coroutine and CANNOT be thread-`target`'d — fully drop the
  threading scaffold and rewrite as the async-concurrency smoke below (drive
  `start_worker`/`arun_worker` on the loop). (Verified live:
  `start_worker(concurrency=4)` ran 4 `asyncio.sleep(0.5)` tasks in ~0.52s vs ~2s
  serial.)

**New tests (the new capability):**

- **async task runs e2e:** an `@task async def` that writes a row (via `sync_to_async`
  ORM or `await Model.objects.acreate`); enqueue; `run_absurd_worker()` (burst); assert
  the row
  - result snapshot `completed`.
- **sync + async in one worker run:** enqueue one `def` and one `async def` task; single
  burst worker runs both; both complete.
- **sync task still behaves:** an existing sync-task test already covers this (executor
  path) — assert it stays green.
- **async failure recorded + retried:** an `async def` that raises → snapshot `failed`,
  errors recorded (mirrors `boom`).
- **takes_context for async:** an `async def` task with `takes_context=True` gets a
  `TaskContext` with the right `attempt`.
- **async concurrency smoke:** enqueue N async tasks that each `await asyncio.sleep`;
  blocking/burst worker with `concurrency>1` completes them in roughly-concurrent (not
  serial) wall-clock — proves loop concurrency. Keep the sleep tiny + the assertion
  generous to avoid flakiness.

No mocks; real DB: `PGPORT=5433 docker compose up -d db`, run with `PGPORT=5433`.

## Out of scope (deferred)

Native async `aenqueue` (produce side, `AsyncAbsurd`/`AsyncConnection`); separate
async-vs-executor concurrency knobs (one `--concurrency` for now); multi-DB; the other
deferred follow-ups (ALWAYS_EAGER, public register hook to drop the `_registry` SLF001).
Also noted (separate, from the Tasks-API refresh): `TaskResult.id` documented
`< 64 chars` while SP6's `queue:task_id` can exceed it for long queue names — latent SP6
caveat, its own follow-up.
