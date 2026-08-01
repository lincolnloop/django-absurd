# `run_after` via a wrapper task — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Defer a task by enqueueing a separate wrapper run that sleeps and then
enqueues the real task, so Absurd's `cancellation` rules measure the caller's task
rather than the wait.

**Architecture:** Absurd's `spawn` takes no `available_at`. This branch currently claims
the caller's task at t=0 and sleeps inside it, which sets `first_started_at` before any
work exists and breaks both cancellation rules. Replacement: `enqueue` spawns a row
named `django_absurd:defer:<target path>`, which **is not a Django `@task`** —
`LazyTaskRegistry` recognises the prefix and builds a handler for it. The handler sleeps
until due, then enqueues the target with the caller's options as a checkpointed step.
`get_result` on the caller's id reports `READY` while waiting and follows the wrapper's
completion payload afterwards.

**Tech Stack:** Django 6.0 Tasks, `absurd_sdk`, psycopg 3, pytest + the `dj_absurd`
fixture.

## Why there is no `@task` — read this before touching Task 1

Two measured facts killed the obvious design. **Do not reintroduce a module-level
`@task`.**

1. A module-level `@task` reads `settings.TASKS`, raising
   `ImproperlyConfigured: Requested setting TASKS` on import. `django_absurd.test` must
   import with no settings at all.
2. Worse: `@task` resolves `task_backends["default"]` and validates its queue at
   **decoration** time. Measured with
   `TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["reports"]}}`:

   ```
   InvalidTask: Queue 'default' is not valid for backend.
   ```

   So any project whose `QUEUES` omits `"default"`, or whose `TASKS` alias isn't
   `"default"`, would crash — at dispatch, inside `LazyTaskRegistry.get`, where the
   `except ImportError` does **not** catch `InvalidTask`, taking the worker down. A
   `@task`'s `queue_name` is fixed at decoration and is unrelated to the `queue` we pass
   to `spawn`, so spawning onto the right queue does not help.

Hence: a synthetic `task_name`, resolved by our own registry. It also gives per-target
filtering in the admin's Tasks changelist, which is the point of the name carrying the
target.

## Global Constraints

- Floor: Django 6.0 / Python 3.12. `uv run pre-commit run --all-files` owns ruff and
  mypy — never invoke either directly.
- **`django_absurd.test` must import with NO Django settings configured.** Verify with
  the import oracle after every task.
- **Never add a ruff `noqa`/ignore or a coverage pragma.** If you think one is
  unavoidable, stop and report rather than adding it.
- `import typing as t`, `import datetime as dt`, absolute imports only. Functions
  contain a verb. Helpers BELOW their callers. No leading-underscore module-level names.
- Re-raise inside `except` with `from exc`. Exceptions own their messages, under
  `DjangoAbsurdError`.
- Tests: function-based; assert COMPLETE error messages; alphabetize parametrize values
  and each test's own fixture parameters; reach fixture tasks module-qualified
  (`tasks.add`); never unit-test an internal helper; don't wrap two lines in a helper.
- Durable tests freeze through `dj_absurd.freeze_time()`, never `time.sleep`.
- 100% coverage on lines this plan adds or changes.
- Never `commit --amend`; every change is a new commit. No AI attribution in messages.

## Verified interfaces (measured — do not re-derive)

- `absurd_sdk.Absurd.spawn(task_name, params, max_attempts, retry_strategy, headers, queue, cancellation, idempotency_key)`
  — **no `available_at`, no `task_id`.**
- `AsyncTaskContext`:
  `await_event, await_task_result, begin_step, complete_step, emit_event, headers, heartbeat, sleep_for, sleep_until, step`.
  **No `spawn`.**
- **An enqueue performed inside a task body runs in the SAME drain.** Proven live:
  `[(run_deferred, completed, "default:<uuid>"), (tests.tasks.add, completed, 3)]`.
  Every test below relies on it, and `tests/core/test_absurd_fixture.py`'s
  `spawn_child_then_return` already pins the same-drain behaviour.
- **A task's return value lands in `completed_payload` as the plain value** — the
  wrapper's returned id string arrives as a string, so the redirect can use it directly.
- `absurd_params(**merged_dict).bind(task)` round-trips the caller's options — verified:
  `max_attempts=9` and `headers={"trace": "abc"}` came back exactly.
- `LazyTaskRegistry.get` (`worker.py`) is the single dispatch-resolution hook; its entry
  shape is
  `{"name", "queue", "default_max_attempts", "default_cancellation", "handler"}`.

## Decisions already taken (do not relitigate)

- Synthetic, per-target name: `django_absurd:defer:<target dotted path>`.
- The wrapper spawns onto the **target's** queue, or a worker consuming only that queue
  would never run it.
- Caller options pass through the wrapper's params; the wrapper itself gets none of
  them.
- The inner enqueue is a checkpointed step — effectively-once across wrapper retries.
- Status: `READY` while the wrapper sleeps (**always**, including retry backoff — Django
  has no status for "the deferral is retrying", and READY is the truthful answer to "has
  the caller's task started"), the inner task's result once the wrapper completes, the
  wrapper's own `FAILED` if the deferral exhausts attempts. While READY, pass any
  wrapper failure through to `errors` so a struggling launch is visible without
  misreporting status.
- **Accepted and to be documented, not fixed:** a wrapper swept by `cleanup_ttl` after
  wake makes the caller's id raise `TaskResultDoesNotExist` though the real task lives;
  and two deferred enqueues sharing one `idempotency_key` create two wrapper rows,
  deduping at wake rather than at enqueue.

## File structure

| File                                           | Responsibility                                                                                                                                                                                                                                                                                                                     |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `django_absurd/worker.py`                      | `DEFER_NAME_PREFIX`, the registry branch that recognises it, and `build_deferred_handler`. Deletes `sleep_until_run_after`, `DEFER_STEP_NAME`, the `RUN_AFTER_HEADER` import, and the injected sleep in `handler`. Dispatch machinery belongs here beside `build_handler`; no new module, so nothing new lands on any import path. |
| `django_absurd/backends.py`                    | Spawn the wrapper when `run_after` is set; redirect `get_result`. Deletes `RUN_AFTER_HEADER`, `waits_for_its_run_after`, `warn_if_cancellation_misreads_the_wait` and its dedupe set. Keeps `normalize_to_utc`.                                                                                                                    |
| `tests/core/test_run_after.py`                 | Rewritten. Every existing test's disposition is listed in Task 3.                                                                                                                                                                                                                                                                  |
| `docs/web/tasks.md`, `django_absurd/AGENTS.md` | Drop the cancellation caveat; document the wrapper row and the two accepted consequences.                                                                                                                                                                                                                                          |

---

### Task 1: Dispatch a synthetic deferred name

**Files:**

- Modify: `django_absurd/worker.py`
- Test: `tests/core/test_run_after.py`

**Interfaces:**

- Produces: `DEFER_NAME_PREFIX = "django_absurd:defer:"`, and a registry entry for any
  `task_name` starting with it whose handler sleeps then enqueues the target. Wrapper
  params are
  `{"args": [], "kwargs": {"args": list, "kwargs": dict, "queue": str, "options": dict, "due": str}}`
  — the target path comes from the **name**, not the params.

- [ ] **Step 1: Write the failing test**

Spawn a wrapper row directly through the SDK client — the enqueue side does not exist
until Task 2, and this is the dispatch contract, driven the way a worker drives it:

```python
def test_a_deferred_name_sleeps_then_enqueues_its_target(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        due = (dj_absurd.now + dt.timedelta(hours=1)).isoformat()
        get_absurd_client().spawn(
            "django_absurd:defer:tests.tasks.add",
            {
                "args": [],
                "kwargs": {
                    "args": [1, 2],
                    "kwargs": {},
                    "queue": "default",
                    "options": {},
                    "due": due,
                },
            },
            queue="default",
        )

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(hours=1))
        ran = [(run.task_name, run.state) for run in dj_absurd.drain()]
        assert ("django_absurd:defer:tests.tasks.add", "completed") in ran
        assert ("tests.tasks.add", "completed") in ran
```

- [ ] **Step 2: Run it to verify it fails**

Run:
`uv run pytest tests/core/test_run_after.py::test_a_deferred_name_sleeps_then_enqueues_its_target -v --no-cov`
Expected: FAIL — the drain returns `[]`, because `LazyTaskRegistry.get` cannot
`import_string` that name and returns `default`, so the run is never dispatched.

- [ ] **Step 3: Teach the registry the prefix**

In `worker.py`, add the constant beside `logger`:

```python
# A deferred enqueue spawns a row under this prefix rather than the caller's task, so the
# caller's task is never claimed before its work exists. Not a Django @task: decorating one
# reads settings.TASKS and validates a queue at import, which breaks any project whose
# QUEUES omits "default". The suffix is the target's dotted path, which makes deferred work
# filterable by target in the admin.
DEFER_NAME_PREFIX = "django_absurd:defer:"
```

In `LazyTaskRegistry.get`, branch before the `import_string` attempt:

```python
        if name not in self:
            if name.startswith(DEFER_NAME_PREFIX):
                self[name] = {
                    "name": name,
                    "queue": self.queue,
                    "default_max_attempts": None,
                    "default_cancellation": None,
                    "handler": build_deferred_handler(
                        name.removeprefix(DEFER_NAME_PREFIX)
                    ),
                }
                return super().get(name, default)
            try:
                task = import_string(name)
```

Add the handler builder BELOW `build_handler`:

```python
def build_deferred_handler(
    target: str,
) -> t.Callable[[TaskParams, AsyncTaskContext], t.Awaitable[JsonValue]]:
    """Handle one deferred run: sleep until due, then enqueue ``target``.

    The enqueue is a checkpointed step, so a retry of this run replays the stored id
    rather than enqueueing a second time. Enqueuing is synchronous Django work, so it
    goes to a thread — the same hop ``handler`` makes for a sync task.
    """

    async def handler(params: TaskParams, ctx: AsyncTaskContext) -> JsonValue:
        spec = params["kwargs"]
        logger.info(
            "django-absurd deferred task waiting: target=%s due=%s task_id=%s",
            target,
            spec["due"],
            ctx.task_id,
        )
        await ctx.sleep_until(
            "django_absurd:defer", dt.datetime.fromisoformat(str(spec["due"]))
        )

        async def enqueue_the_target() -> JsonValue:
            return await asyncio.to_thread(
                enqueue_deferred_target,
                target,
                list(spec["args"]),
                dict(spec["kwargs"]),
                str(spec["queue"]),
                dict(spec["options"]),
            )

        return await ctx.step("django_absurd:enqueue", enqueue_the_target)

    return handler


def enqueue_deferred_target(
    target: str,
    args: list[t.Any],
    kwargs: dict[str, t.Any],
    queue: str,
    options: dict[str, t.Any],
) -> str:
    """Enqueue the caller's task, returning its ``TaskResult.id``."""
    close_old_connections()
    try:
        target_task = import_string(target)
        if target_task.queue_name != queue:
            target_task = target_task.using(queue_name=queue)
        if options:
            target_task = params_module.absurd_params(**options).bind(target_task)
        return str(target_task.enqueue(*args, **kwargs).id)
    finally:
        close_old_connections()
```

`worker.py` already imports `close_old_connections`. Import the params module at the top
— `from django_absurd import params as params_module` — no lazy import and no `noqa`:
`worker.py` is not on the settings-free path (`django_absurd.test` imports it inside
`drain()`), so a top-level import is safe and cheaper.

- [ ] **Step 4: Run it to verify it passes**

Run:
`uv run pytest tests/core/test_run_after.py::test_a_deferred_name_sleeps_then_enqueues_its_target -v --no-cov`
Expected: PASS, with both rows in the second drain.

- [ ] **Step 5: Import oracle**

From a scratch directory OUTSIDE the repo, one plain non-Django test file:
`env -u DJANGO_SETTINGS_MODULE <repo>/.venv/bin/python -m pytest test_plain.py -p no:cacheprovider -q`
Expected: `1 passed`, not INTERNALERROR.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/worker.py tests/core/test_run_after.py
git commit -m "feat: dispatch a deferred run through a synthetic task name"
```

---

### Task 2: Spawn the wrapper on a deferred enqueue

**Files:**

- Modify: `django_absurd/backends.py`, `django_absurd/worker.py`
- Test: `tests/core/test_run_after.py`

**Interfaces:**

- Consumes: `DEFER_NAME_PREFIX` and the params shape from Task 1.
- Produces: `enqueue` returns a `TaskResult` whose id names the **wrapper** row.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_deferred_enqueue_spawns_a_wrapper_named_for_its_target(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    register_jsonb_loader(connections["default"].connection)
    with dj_absurd.freeze_time():
        tasks.add.using(run_after=dj_absurd.now + dt.timedelta(hours=1)).enqueue(1, 2)

    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["task_name"] == "django_absurd:defer:tests.tasks.add"


def test_a_deferred_task_keeps_the_callers_per_call_options(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # bind() options are per-invocation, so they must survive the hop to the inner spawn.
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        absurd_params(max_attempts=9, headers={"trace": "abc"}).bind(tasks.add).using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        dj_absurd.drain()

    # The inner task ran inside that second drain, so read its ROW — nothing is claimable.
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "select max_attempts, headers from absurd.t_default "
            "where task_name = 'tests.tasks.add'"
        )
        assert cursor.fetchone() == (9, {"trace": "abc"})


def test_a_deferred_task_runs_on_the_callers_queue(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # Both rows must land on the target's queue, or a worker consuming only that queue
    # never runs the wrapper.
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        tasks.on_reports.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue()
        assert [run.queue for run in dj_absurd.drain("reports")] == ["reports"]
        frozen_time.shift(dt.timedelta(hours=1))
        assert {run.queue for run in dj_absurd.drain("reports")} == {"reports"}


def test_a_deferred_task_routed_off_its_own_queue_lands_where_it_was_sent(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # tasks.add's own queue is "default"; routing it to "other" is what makes the wrapper
    # re-route the inner enqueue. A target that already declares the queue it is sent to
    # (tasks.on_reports above) leaves that branch untaken, so this is the case that
    # covers `if target_task.queue_name != queue` in enqueue_deferred_target.
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        tasks.add.using(
            queue_name="other", run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        assert [run.queue for run in dj_absurd.drain("other")] == ["other"]

        frozen_time.shift(dt.timedelta(hours=1))
        ran = dj_absurd.drain("other")
        assert [run.queue for run in ran] == ["other", "other"]
        assert ("tests.tasks.add", 3) in [(run.task_name, run.result) for run in ran]
```

- [ ] **Step 2: Run them to verify they fail**

Run:
`uv run pytest tests/core/test_run_after.py -k "spawns_a_wrapper or per_call_options or callers_queue" -v --no-cov`
Expected: FAIL — the first sees `task_name == "tests.tasks.add"`.

Check `tests/tasks.py` for a `reports`-queue task before writing the third test; if
`tasks.on_reports` is not the right name, use whichever exists and say so in your
report.

- [ ] **Step 3: Spawn the wrapper**

In `enqueue`, replace the whole `if task.run_after is not None:` block (the
`RUN_AFTER_HEADER` write and the `warn_if_cancellation_misreads_the_wait` call) with a
branch choosing what to spawn. The caller's options ride in the wrapper's params; the
wrapper gets only its own `max_attempts`:

```python
        if task.run_after is not None:
            spawn_name = f"{DEFER_NAME_PREFIX}{task.module_path}"
            spawn_params = {
                "args": [],
                "kwargs": {
                    "args": list(args),
                    "kwargs": dict(kwargs),
                    "queue": task.queue_name,
                    "options": dict(merged),
                    "due": normalize_to_utc(task.run_after).isoformat(),
                },
            }
            merged = {"max_attempts": self.default_max_attempts}
        else:
            spawn_name = task.module_path
            spawn_params = {"args": list(args), "kwargs": dict(kwargs)}
```

Both `client.spawn(...)` call sites then pass `spawn_name` and `spawn_params` in place
of `task.module_path` and the inline params dict. **The queue is unchanged** — the
existing `queue=task.queue_name` argument already sends the wrapper to the caller's
queue, so nothing here hardcodes `"default"`.

`backends.py` cannot import `worker.py` — `worker` imports `backends`, so that closes a
cycle. Do **not** duplicate the literal. Move it to a leaf module both import, created
in Task 1:

```python
# django_absurd/deferred.py
"""Where a deferred enqueue's name comes from.

A constant and nothing else, so both ``backends`` (which spawns the row) and ``worker``
(which dispatches it) can import it without a cycle and without touching settings.

There is deliberately no ``@task`` here: decorating one resolves
``task_backends["default"]`` and validates its queue at decoration time, so a project
whose ``QUEUES`` omits ``"default"`` would raise ``InvalidTask`` on import — at dispatch,
where it takes the worker down. The handler is built by ``worker.LazyTaskRegistry``
instead.
"""

DEFER_NAME_PREFIX = "django_absurd:defer:"
```

Delete from `backends.py`: `RUN_AFTER_HEADER`, `warn_if_cancellation_misreads_the_wait`,
`WARNED_DEFERRED_CANCELLATION_PATHS`, and the `logging` import plus `logger` if nothing
else uses them. Keep `normalize_to_utc`.

Delete from `worker.py`: `sleep_until_run_after`, its call inside `handler`,
`DEFER_STEP_NAME`, and the `RUN_AFTER_HEADER` import.

**Careful with `DEFER_STEP_NAME`.** Task 1's `build_deferred_handler` passes its sleep
step name as a literal precisely so this deletion is safe. Delete the constant; do
**not** rewrite the handler's literal to reference it, and do not mistake the handler's
own `sleep_until` call for the one being removed — they are different code paths that
happen to share a step name.

- [ ] **Step 4: Run the three tests to verify they pass**

Run:
`uv run pytest tests/core/test_run_after.py -k "spawns_a_wrapper or per_call_options or callers_queue" -v --no-cov`
Expected: PASS. Other tests in the file still fail — Task 3 owns them.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py django_absurd/worker.py tests/core/test_run_after.py
git commit -m "feat: spawn a deferred wrapper instead of sleeping inside the caller's task"
```

---

### Task 3: `get_result` follows the wrapper, and the old tests are settled

**Files:**

- Modify: `django_absurd/backends.py`
- Test: `tests/core/test_run_after.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_waiting_deferred_task_reads_as_ready_with_no_start_times(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time():
        result = tasks.add.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        dj_absurd.drain()

        waiting = backend.get_result(result.id)
        assert waiting.status is TaskResultStatus.READY
        assert waiting.started_at is None
        assert waiting.last_attempted_at is None
        assert waiting.args == [1, 2]          # the caller's args, not the wrapper's


def test_a_woken_deferred_task_reports_the_targets_own_result(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.add.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        dj_absurd.drain()

        done = backend.get_result(result.id)
        assert done.status is TaskResultStatus.SUCCESSFUL
        assert done.return_value == 3


def test_a_deferral_that_cannot_launch_reports_failed(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # A wrapper naming a target that does not import can never launch it. Once it is out
    # of attempts the caller sees FAILED — the deferral failed, and nothing of theirs ran.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        get_absurd_client().spawn(
            "django_absurd:defer:tests.tasks.does_not_exist",
            {
                "args": [],
                "kwargs": {
                    "args": [],
                    "kwargs": {},
                    "queue": "default",
                    "options": {},
                    "due": (dj_absurd.now + dt.timedelta(hours=1)).isoformat(),
                },
            },
            queue="default",
            max_attempts=1,
        )
        drained = dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        drained = dj_absurd.drain()

        result_id = f"default:{drained[0].task_id}"
        assert backend.get_result(result_id).status is TaskResultStatus.FAILED
```

- [ ] **Step 2: Run them to verify they fail**

Run:
`uv run pytest tests/core/test_run_after.py -k "reads_as_ready or targets_own_result or cannot_launch or prefixes_agree" -v --no-cov`
Expected: the woken test reports the wrapper's own return value (an id string) rather
than `3`.

- [ ] **Step 3: Add the redirect**

In `get_result`, branch on a completed wrapper before building a result:

```python
        task, run, worker_ids = fetch_task_and_run(
            self.database, queue, task_id, result_id
        )
        if task.task_name.startswith(DEFER_NAME_PREFIX) and task.state == "completed":
            # Its payload is the id of the task it enqueued, so the caller's id keeps
            # describing their own task for the rest of its life.
            return self.get_result(str(task.completed_payload))
        return build_task_result(self, result_id, task, run, worker_ids)
```

In `build_task_result`, insert immediately after the block of local assignments and
**before** the `try: task_obj = import_string(task_name)` below it — `task_name` is
rebound, so order matters:

```python
    is_deferred_wrapper = task_name.startswith(DEFER_NAME_PREFIX)
    if is_deferred_wrapper:
        # The caller's id names the wrapper, but their TaskResult must describe THEIR
        # task: its path is the name's suffix and its call is in the wrapper's kwargs.
        task_name = task_name.removeprefix(DEFER_NAME_PREFIX)
        spec = params["kwargs"]
        params = {"args": list(spec["args"]), "kwargs": dict(spec["kwargs"])}
        # Claimed at t=0 only so it could sleep — nothing of the caller's has started.
        first_started_at = None
        run_started = None
```

and on the existing `status` line:

```python
    status = map_state_to_status(state)
    if is_deferred_wrapper and state == "sleeping":
        # No Django status means "the deferral is retrying", and READY is the honest
        # answer to whether the caller's task has started. A failing launch stays visible
        # through `errors` below, and ends FAILED once out of attempts.
        status = TaskResultStatus.READY
```

**Do not widen the `errors` block** — the earlier draft said to, and its premise is
false. `absurd.fail_run` inserts the retry run with `failure_reason` NULL and repoints
`last_attempt_run` at it, so a wrapper in backoff has no failure to surface (measured:
`sleeping / attempts=2 / READY / errors=[]` with and without the change). A struggling
launch is therefore invisible to `get_result` until attempts run out and it reports
FAILED; it is visible on the wrapper's own admin row throughout.

Delete `waits_for_its_run_after` — the prefix check replaces it, which removes the
aware-clock comparison and its `USE_TZ` handling.

- [ ] **Step 4: Settle every existing test in the file**

The file has eleven tests. Disposition for each — do not leave any as-is without
checking:

| Test                                                           | Action                                                                                                                                                                                 |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_run_after_is_accepted_now_that_defer_is_supported`       | keep, unchanged                                                                                                                                                                        |
| `test_run_after_rides_a_namespaced_spawn_header`               | **delete** — the header no longer exists; Task 2's wrapper-name test replaces it                                                                                                       |
| `test_run_after_merges_with_a_caller_s_own_headers`            | **delete** — we never touch the caller's headers now; Task 2's per-call-options test covers what it protected                                                                          |
| `test_a_run_after_already_past_runs_on_the_first_drain`        | **rewrite** — the drain now returns wrapper + target, so assert both, and read state through the backend                                                                               |
| `test_a_deferred_task_sleeps_until_run_after_then_runs_once`   | **rewrite** — second drain returns two runs, and `.attempts` would read the wrapper's                                                                                                  |
| `test_a_deferred_task_runs_its_own_steps_exactly_once`         | **rewrite** — second drain returns wrapper `completed` + target `sleeping`. Still worth keeping: it proves the caller's own steps are untouched now that nothing is injected into them |
| `test_a_waiting_deferred_task_reports_no_start_times`          | **delete** — folded into Step 1's `reads_as_ready_with_no_start_times`                                                                                                                 |
| `test_a_deferred_task_survives_a_project_that_disables_use_tz` | **rewrite** — still load-bearing (the wrapper's `sleep_until` needs an aware instant, and `normalize_to_utc` is what supplies it), but its drain assertion breaks                      |
| `test_deferring_a_task_that_sets_cancellation_warns_once`      | **delete** — the warning is gone, because the bug it warned about is gone                                                                                                              |
| `test_a_deferred_task_reads_as_ready_while_it_waits`           | **delete** — replaced by Step 1's version                                                                                                                                              |
| `test_a_woken_deferred_task_no_longer_reads_as_ready`          | **delete** — replaced by Step 1's `targets_own_result`, which asserts more                                                                                                             |

- [ ] **Step 4b: Fix two tautological queue assertions Task 2 flagged**

`RunSnapshot.queue` is the queue passed to `drain()`, not a per-run column
(`test.py:363` — `queue=queue`). So
`assert [run.queue for run in drain("reports")] == ["reports"]` cannot fail, and both
queue tests from Task 2 assert it. The real evidence is already there: a drain of one
queue only claims rows from that queue, so finding the rows there _is_ the proof.
Replace the `run.queue` assertions with task names:

```python
    # in test_a_deferred_task_runs_on_the_callers_queue
        assert [run.task_name for run in dj_absurd.drain("reports")] == [
            "django_absurd:defer:tests.tasks.on_reports"
        ]
        frozen_time.shift(dt.timedelta(hours=1))
        assert sorted(run.task_name for run in dj_absurd.drain("reports")) == [
            "django_absurd:defer:tests.tasks.on_reports",
            "tests.tasks.on_reports",
        ]

    # in test_a_deferred_task_routed_off_its_own_queue_lands_where_it_was_sent
        # drop the `[run.queue for run in ran] == ["other", "other"]` line; the
        # ("tests.tasks.add", 3) membership assertion below it is the load-bearing one
```

- [ ] **Step 5: Prove both cancellation bugs are fixed**

These two are why the plan exists. Both fail on the old mechanism.

```python
def test_a_deferred_tasks_max_duration_measures_its_body_not_its_wait(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        result = absurd_params(cancellation={"max_duration": 60}).bind(tasks.add).using(
            run_after=dj_absurd.now + dt.timedelta(hours=2)
        ).enqueue(1, 2)
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=2))
        dj_absurd.drain()

        assert backend.get_result(result.id).return_value == 3


def test_a_deferred_tasks_max_delay_measures_from_its_due_time(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # max_delay is "cancel if not started within N of enqueue". It must count from when
    # the task was really enqueued — at wake — not from the caller's deferred enqueue.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        result = absurd_params(cancellation={"max_delay": 300}).bind(tasks.add).using(
            run_after=dj_absurd.now + dt.timedelta(hours=2)
        ).enqueue(1, 2)
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=2))
        dj_absurd.drain()

        assert backend.get_result(result.id).return_value == 3
```

Run both. If the second passes trivially (because the inner task runs immediately at
wake and never has a chance to be late), say so in your report rather than dressing it
up — a test that cannot fail is worth knowing about.

- [ ] **Step 6: Decide the fixture's own read path**

`dj_absurd.get_result` (`django_absurd/test.py`) is a **separate** read path from the
backend's and is NOT redirected by anything above. A deferred id read through it reports
the raw wrapper row: `task_name` with the prefix, `args`/`kwargs` from the wrapper's
params, `result` the inner id string.

Leave it un-redirected — a test facade reporting the row that exists is defensible, and
the fixture is for inspecting real state. But **add a test pinning that behaviour** so
it is a decision rather than an accident, and a sentence to `docs/web/testing.md`'s
`get_result` section. If you think it should redirect instead, stop and report rather
than deciding alone.

- [ ] **Step 7: Commit**

```bash
git add django_absurd/backends.py django_absurd/test.py tests/core/test_run_after.py docs/web/testing.md
git commit -m "feat: report a deferred task's real result through its wrapper"
```

---

### Task 4: Docs, then every gate

**Files:**

- Modify: `docs/web/tasks.md`, `django_absurd/AGENTS.md`

- [ ] **Step 1: Delete the cancellation caveat from both**

`tasks.md`'s admonition ("`run_after` and `cancellation` measure the wait differently")
and `AGENTS.md`'s "Don't combine `run_after` with `cancellation`" describe a limitation
that no longer exists.

- [ ] **Step 2: Document what a reader will now see**

In `tasks.md`'s "Run it later", replace the claimed-at-enqueue paragraph with: a
deferred enqueue creates a second row named `django_absurd:defer:<your task path>` that
waits and then enqueues theirs, both visible in the admin and filterable by that name;
the id `enqueue` returned keeps working throughout — `READY` while waiting, then their
task's own status and return value. Plus the two accepted consequences, one sentence
each:

- a very short `cleanup_ttl` can sweep the wrapper after it wakes, in which case the id
  raises `TaskResultDoesNotExist` even though the task ran;
- two deferred enqueues sharing one `idempotency_key` produce two wrapper rows and
  dedupe when they wake, rather than at enqueue.

Mirror into `AGENTS.md`, keeping it terse — that file is a user guide, not an internals
tour.

- [ ] **Step 3: Build the docs site**

Run: `uvx zensical build` Expected: `No issues found`. The site slugifies
`## Retries & spawn options` to `#retries-spawn-options` (single hyphen), not GitHub's
double.

- [ ] **Step 4: Run every gate**

```bash
uv run pytest tests/core -q --no-cov -n4 --timeout=120
uv run pytest tests/pg_cron -q --no-cov -n4 --timeout=120   # 258
uv run pytest tests/multidb -q --no-cov                     # 7
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
uv run pytest tests/core -q --no-cov --create-db --timeout=120
```

`tests/core` is 451 before this plan; six deletions and the new tests move it, so report
the actual number rather than checking it against a prediction. Then the import oracle
again, the GUC-leak check on both ports
(`select datname, setconfig from pg_db_role_setting s join pg_database d on d.oid = s.setdatabase;`
→ 0 rows), and `uv run coverage report --include='django_absurd/*' --show-missing` to
confirm 100% on changed lines.

- [ ] **Step 5: Rewrite the PR body**

PR #135 describes the mechanism this plan replaces — header, injected sleep, and the
cancellation limitation presented as an accepted trade. It is now actively misleading.
Rewrite it around the wrapper, and say why the design is what it is: `spawn` has no
`available_at`, a library-shipped `@task` cannot exist, and the cancellation defects
came from claiming the caller's task early.

- [ ] **Step 6: Commit**

```bash
git add docs/web/tasks.md django_absurd/AGENTS.md
git commit -m "docs: describe deferred enqueue as a wrapper row"
```

---

## Notes for the implementer

- **A hang means Postgres is ahead of Python.** SIGKILL, then
  `PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d postgres -c 'alter database "absurd_test_core_gw0" reset "absurd.fake_now";'`
  (adjust `_gwN`, or drop the suffix for the serial database).
- Both compose services must be up: `docker compose up -d db db_pg_cron` from the repo
  root. They do not survive a restart; a refused connection means a stopped container.
- `run_deferred.using(run_after=...)` — a caller deferring the wrapper itself —
  terminates after one extra hop, and `get_result` then walks two levels. No guard is
  planned. If you find it loops or misreports, report it.
- If any test here cannot be made to pass **as specified**, stop and report rather than
  adjusting the assertion until it goes green. Two of this plan's predecessors were
  wrong about what a drain returns, and the tests are what caught it.
