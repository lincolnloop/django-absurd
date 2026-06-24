# django-absurd — Spec: Tasks-API produce side / enqueue (SP2)

Date: 2026-06-19 Status: approved-for-planning

Second sub-project of the Django Tasks integration. SP1 built the config: an
`AbsurdBackend(BaseTaskBackend)` in `TASKS` carrying queue config, with `enqueue`/
`get_result` left as `NotImplementedError`. SP2 implements the **produce side**:
`AbsurdBackend.enqueue` → `client.spawn(task.module_path, …)`, returning a Django
`TaskResult`. Result retrieval, native async, defer, and priority are deliberately
deferred (see Out of scope + the `deferred-tasks-api-followups` note).

## Why this scope

Absurd's only result API is `absurd.get_task_result(queue, task_id)` →
`{state, result, failure}`; there is no public SDK call to read back a task's name /
args / kwargs / timestamps from a `result_id`, but Django's `TaskResult` dataclass
requires all of them. So full `get_result` can't be reconstructed from the SDK alone —
it becomes its own future sub-project (read-model vs SDK support).
`supports_get_result=False` is valid (Django's `ImmediateBackend` does the same). Async
execution is decided by the worker (SP3), so building async-enqueue plumbing now is
premature. SP2 therefore ships sync `enqueue` only — the smallest produce-side slice
that lets `my_task.enqueue(...)` write a real Absurd task row.

## Support flags (`AbsurdBackend`)

- `supports_get_result = False` — result retrieval deferred.
- `supports_async_task = False` — coroutine task functions rejected at `enqueue` until
  the worker (SP3) can run them.
- `supports_defer = False` — `run_after`/delayed tasks rejected (Absurd schedules via a
  separate path; not wired yet).
- `supports_priority = False` — Absurd has no priority.

These four gate Django's `validate_task`, so unsupported tasks fail fast at enqueue with
Django's own `InvalidTask`.

## `enqueue(self, task, args, kwargs) -> TaskResult`

1. `self.validate_task(task)` — `BaseTaskBackend` checks the func is module-level,
   rejects coroutine funcs (since `supports_async_task=False`), and enforces the queue
   allowlist (`task.queue_name in self.queues`).
2. `client = get_absurd_client(self.database)` — the SYNC client, reusing Django's
   psycopg connection (`connections[self.database].connection`). Because `spawn`
   executes on that shared connection, the task-row INSERT runs inside Django's current
   transaction → **enqueue-on-commit is automatic** (the row persists iff the
   surrounding transaction commits; it rolls back with it). `get_absurd_client` already
   asserts the psycopg3 backend (raises `ImproperlyConfigured` otherwise).
3. `spawn = client.spawn(task.module_path, {"args": list(args), "kwargs": kwargs}, queue=task.queue_name, max_attempts=self.default_max_attempts)`.
   The SDK `json.dumps`'s the params; Django already requires args/kwargs to be JSON
   round-trippable, so no extra serialization. `spawn` returns
   `{"task_id", "run_id", "attempt"}`.
4. Return:
   ```python
   TaskResult(
       task=task,
       id=spawn["task_id"],
       status=TaskResultStatus.READY,
       enqueued_at=timezone.now(),
       started_at=None, finished_at=None, last_attempted_at=None,
       args=list(args), kwargs=dict(kwargs),
       backend=self.alias, errors=[], worker_ids=[],
   )
   ```

`task.module_path` is the dotted import path of the task function — the globally-unique,
importable name the SP3 worker will resolve back to the callable.

### `aenqueue`

Not overridden. `BaseTaskBackend.aenqueue` wraps `enqueue` via
`sync_to_async(self.enqueue, thread_sensitive=True)`, which is correct for the
shared-connection sync client. (Native async enqueue is deferred.)

### Point-of-use errors

- Wrong DB engine → `ImproperlyConfigured` (via `get_absurd_client`).
- Undeclared queue (`task.queue_name` not in `self.queues`) → Django `InvalidTask` from
  `validate_task`.
- **Declared-but-unsynced queue / unmigrated schema** → `absurd.spawn_task` raises at
  the DB (`ProgrammingError` for absent schema; a queue-missing error otherwise).
  `enqueue` catches these and re-raises with a clear, actionable message naming the
  queue and pointing at `manage.py absurd_sync_queues` / `manage.py migrate`. (Don't
  swallow — wrap and re-raise.)

## Testing (pytest, function-based, real Postgres via compose; single-DB suite)

New `tests/tasks.py` holds real `@task` functions bound to the default (Absurd) backend
(e.g. `add(a, b)`, an `async def` task for the rejection test). `tests/settings.py`
already configures the default `TASKS` alias as `AbsurdBackend` with
`QUEUES=["default"]`. Each enqueue test runs `call_command("absurd_sync_queues")` first,
because the autouse `_reset_absurd_queues` fixture drops all queues per-test. Tests use
`@pytest.mark.django_db(transaction=True)` (spawn commits + queue DDL).

**Observability:** `fetch_task_result` returns only state, so to verify what was spawned
a test claims it back: `get_absurd_client().claim_tasks(batch_size=1)` returns rows
carrying `task_name` and `params`. (`claim_tasks` claims from the client's `queue_name`
= `"default"`, matching the tasks' default queue.)

Cases:

- **Lands + TaskResult shape:** `r = add.enqueue(1, 2)` → `r.id` is a non-empty str,
  `r.status == TaskResultStatus.READY`, `r.args == [1, 2]`, `r.kwargs == {}`,
  `r.backend == "default"`; claiming the task yields `task_name == "tests.tasks.add"`
  and `params == {"args": [1, 2], "kwargs": {}}`.
- **kwargs preserved:** `add.enqueue(a=1, b=2)` → claimed
  `params["kwargs"] == {"a": 1, "b": 2}`.
- **Enqueue-on-commit (transaction sharing):** inside a `transaction.atomic()` block
  that raises (rolls back), call `add.enqueue(...)`; after rollback, `claim_tasks`
  returns nothing — proving the spawn rode Django's transaction.
- **Async task rejected:** an `async def` `@task` → `.enqueue()` raises `InvalidTask`
  (`supports_async_task=False`).
- **Undeclared queue rejected:** `add.using(queue_name="nope").enqueue(1, 2)` raises
  `InvalidTask` (allowlist; `"nope"` not in `QUEUES`).
- **Declared-but-unsynced → clear error:** declare a queue in `TASKS` but do NOT sync
  it, then `enqueue` to it → raises the wrapped error whose text names the queue and
  `absurd_sync_queues`.
- **aenqueue works:** `asyncio.run(add.aenqueue(1, 2))` → the task row lands
  (claimable), exercising the base `sync_to_async` path.

## Files

- Modify: `django_absurd/backends.py` — implement `enqueue`; set the four support flags;
  remove the `enqueue` `NotImplementedError` stub. (`get_result`/`aget_result` stay
  unimplemented; `supports_get_result=False` keeps Django from calling them.)
- Create: `tests/tasks.py` — real `@task` definitions for the tests.
- Create: `tests/test_enqueue.py` — the cases above.

## Out of scope (future sub-projects, logged)

- **Result retrieval** — `get_result`/`aget_result`, `supports_get_result`. Needs a
  Django read-model (`task_id` → module_path/args/kwargs/enqueued_at) merged with the
  result snapshot, or SDK support. Its own sub-project.
- **SP3 — consume side:** the `absurd_worker` command that claims tasks, resolves
  `module_path` to the `@task` callable, runs it, and records the result; flipping
  `supports_async_task` once the async worker exists.
- **Native async enqueue** (`AsyncAbsurd` + `AsyncConnection`), `run_after`/defer,
  priority, idempotency keys.
