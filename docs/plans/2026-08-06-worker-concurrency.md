# One Worker Mode, With Working Concurrency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `absurd_worker --concurrency N` keeps N tasks in flight instead of stalling on
a batch barrier, and `--burst` — test scaffolding that leaked onto the CLI — is deleted.

**Architecture:** Replace the `client.start_worker(...)` delegation in
`run_blocking_worker` with a local rolling-window loop over the public `claim_tasks`,
ported from
[upstream absurd PR #137](https://github.com/earendil-works/absurd/pull/137). Loop
carries its own `asyncio.Event` stop handle, since the SDK's `_worker_running` flag is
only read by the SDK's own loop. Burst is removed after every test that used it moves to
`dj_absurd.drain()` (execution) or a live-worker helper (command assertions), so the
suite is green at every commit.

**Tech Stack:** Python 3.12+, Django 6.0+, `absurd-sdk>=0.4.0,<0.5.0`, psycopg3,
asyncio, pytest + pytest-django.

**Spec:**
[`docs/specs/2026-08-06-worker-concurrency-design.md`](../specs/2026-08-06-worker-concurrency-design.md).
Read it before Task 1 — it records why each decision is what it is.

## Global Constraints

- Floor Django 6.0 / Python 3.12; psycopg (v3) backend only.
- `import typing as t`, never `from typing import X`. Absolute imports only.
- Functions carry a verb. No leading-underscore module constants/helpers. Helpers go
  BELOW their caller.
- Tests: pytest, function-based only, no class-based. No `unittest.mock.patch`, no
  monkeypatching. Test through real entrypoints; never unit-test an internal helper.
- Test fixture tasks defined locally in a test module must carry the verb themselves
  (`hold_until_released`, not `slow`).
- Alphabetize `@pytest.mark.parametrize` values and a test's own fixture parameters.
- Assert COMPLETE message text, never a fragment.
- Never add a ruff `noqa` or ignore without asking first.
- Never `git commit --amend`. Every change is a new commit.
- Iteration gate per task: `uv run pytest <path> -q --no-cov`, then
  `uv run pre-commit run --all-files` (owns ruff + mypy + prettier — never invoke them
  directly). `git add` new files first: `--all-files` skips untracked.
- **Run only the test modules your task touched.** Never `pytest tests/core` for a
  single edit — coverage instrumentation dominates and the whole-suite run belongs to
  the coordinator gate at the end. Each task below names its exact target modules; run
  those. `tests/multidb` and `tests/pg_cron` touch none of this work (neither imports
  `run_absurd_worker`; pg_cron's only worker call asserts the `--beat` refusal, which
  raises before the loop) — leave them to the coordinator.
- The full `uvx --with tox-uv tox -e dev` gate belongs to the coordinator after the
  commit, NOT to the task implementer — it exceeds a subagent's foreground limit.
- Postgres must be up before any suite: `docker compose up -d db db_pg_cron`.

---

## File Structure

**Modified:**

- `django_absurd/worker.py` — refill loop, `stop=` handle, burst removal. The whole
  change to production behavior lives here.
- `django_absurd/management/commands/absurd_worker.py` — drop `--burst` and the
  `--burst`/`--beat` conflict check.
- `django_absurd/test.py`, `django_absurd/pytest_plugin.py` — prose only ("burst drain"
  → "drain").
- `django_absurd/AGENTS.md`, `docs/web/testing.md` — one worker mode; honest
  `--concurrency`.
- `tests/utils.py` — `run_absurd_worker` switches to the drain; new
  `run_worker_command_until` helper for command-level assertions.
- ~20 test modules — migrate off `burst=True` (full inventory in Task 3 / Task 4).

**Created:**

- `tests/core/test_worker_slot_refill.py` — refill, stop, and shutdown behavior.

---

### Task 1: Refill loop and its own stop handle

**Files:**

- Create: `tests/core/test_worker_slot_refill.py`
- Modify: `django_absurd/worker.py:484-503` (`run_blocking_worker`),
  `django_absurd/worker.py:505-519` (`run_worker_with_beat`)
- Modify: `tests/core/test_worker.py:446-469` (`test_blocking_worker_drains_then_stops`)

**Interfaces:**

- Consumes: `aworker_client(backend, queue)`, `WorkerOptions`,
  `client.claim_tasks(batch_size, claim_timeout, worker_id)`,
  `client._execute_task(claimed, claim_timeout)` — all already in `worker.py`.
- Produces:
  `run_blocking_worker(client: AsyncAbsurd, options: WorkerOptions, *, stop: asyncio.Event | None = None) -> None`.
  Tasks 2 and 4 both drive shutdown through that `stop` keyword.

- [ ] **Step 1: Write the failing refill test**

Create `tests/core/test_worker_slot_refill.py`:

```python
import asyncio

import pytest
from django.core.management import call_command
from django.tasks import task

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.worker import WorkerOptions, aworker_client, run_blocking_worker

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]

HOLD: dict[str, asyncio.Event] = {}
STARTED: dict[str, asyncio.Event] = {}
ORDER: list[str] = []


def get_default_backend() -> AbsurdBackend:
    return get_absurd_backends()["default"]


@task(queue_name="default")
async def hold_until_released(name: str) -> None:
    """Occupy one worker slot until the test lets go of it."""
    ORDER.append(name)
    STARTED[name].set()
    await HOLD["gate"].wait()


@task(queue_name="default")
async def record_started(name: str) -> None:
    ORDER.append(name)
    STARTED[name].set()


def arm_events(*names: str) -> None:
    ORDER.clear()
    STARTED.clear()
    HOLD["gate"] = asyncio.Event()
    for name in names:
        STARTED[name] = asyncio.Event()


def test_worker_starts_a_later_task_while_a_slow_one_still_runs() -> None:
    # A worker of C slots must keep claiming while one slot is busy. Enqueue C+1
    # tasks so the extra one cannot ride in the same claim batch as the slow task:
    # that is what forces a second claim, and a worker that joins its whole batch
    # before claiming again never issues it.
    #
    # Deterministic, not timed: the slow task blocks on an Event the test owns, and
    # each task announces its own start on another. The only clock is the wait_for
    # that turns "never started" into a clean assertion failure instead of a hang.
    # Time cannot be frozen here — a live worker loop and a real thread pool are the
    # subject, so dj_absurd.freeze_time would deadlock rather than help.
    call_command("absurd_sync_queues")
    arm_events("fast-1", "fast-2", "slow")

    hold_until_released.enqueue("slow")
    record_started.enqueue("fast-1")
    record_started.enqueue("fast-2")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(get_default_backend(), "default") as client:

            async def release_once_the_third_task_starts() -> None:
                try:
                    await asyncio.wait_for(STARTED["fast-2"].wait(), timeout=10)
                finally:
                    HOLD["gate"].set()
                    stop.set()

            outcomes = await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=2), stop=stop),
                release_once_the_third_task_starts(),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, TimeoutError
                ):
                    raise outcome

    asyncio.run(drive())

    assert STARTED["fast-2"].is_set(), (
        "the worker never started the third task while the slow one held a slot: "
        f"started {ORDER}"
    )
```

- [ ] **Step 2: Add the one-slot guard test to the same file**

This one must pass BEFORE and AFTER the change — it locks the `concurrency <= 1` fast
path so the refill work cannot quietly turn a single-slot worker into something else.

```python
def test_one_slot_runs_a_claimed_batch_in_order() -> None:
    call_command("absurd_sync_queues")
    arm_events("first", "second", "third")

    record_started.enqueue("first")
    record_started.enqueue("second")
    record_started.enqueue("third")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(get_default_backend(), "default") as client:

            async def stop_once_all_three_ran() -> None:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(STARTED[n].wait() for n in STARTED)),
                        timeout=10,
                    )
                finally:
                    stop.set()

            outcomes = await asyncio.gather(
                run_blocking_worker(
                    client, WorkerOptions(batch_size=3, concurrency=1), stop=stop
                ),
                stop_once_all_three_ran(),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, TimeoutError
                ):
                    raise outcome

    asyncio.run(drive())

    assert ORDER == ["first", "second", "third"]
```

- [ ] **Step 3: Run both tests to verify the first fails and the second passes**

```bash
uv run pytest tests/core/test_worker_slot_refill.py -q --no-cov
```

Expected: `test_worker_starts_a_later_task_while_a_slow_one_still_runs` FAILS with

```
TypeError: run_blocking_worker() got an unexpected keyword argument 'stop'
```

then, once `stop=` exists (Step 4), it fails for the real reason:

```
AssertionError: the worker never started the third task while the slow one held a slot:
started ['slow', 'fast-1']
```

`test_one_slot_runs_a_claimed_batch_in_order` must pass at every point after `stop=`
exists.

- [ ] **Step 4: Implement the stop handle**

In `django_absurd/worker.py`:

- Give `run_blocking_worker` a keyword-only `stop: asyncio.Event | None = None`,
  defaulting to a fresh `asyncio.Event()` when omitted.
- The `SIGINT`/`SIGTERM` handlers set that event instead of calling
  `client.stop_worker()`. Keep the existing `try/finally` that removes both handlers.
- `run_worker_with_beat` creates the event, passes it to `run_blocking_worker`, and
  still sets its `threading.Event` for the beat in the `finally` — one signal stops both
  halves.

Do not read or write `client._worker_running`: the SDK initializes it `False` and only
`start_worker` flips it, so reusing it would mean writing another library's private
attribute.

- [ ] **Step 5: Implement the refill loop**

Replace the `await client.start_worker(...)` call with a local loop, structured exactly
as upstream PR #137 structures the SDK's:

- `effective_batch_size = options.batch_size or options.concurrency`.
- **Fast path**, `concurrency <= 1`: claim `effective_batch_size`, execute the claimed
  tasks sequentially, and when a claim comes back empty sleep `poll_interval`. Without
  this branch, capping claims by free capacity silently reduces
  `--batch-size N --concurrency 1` from one round trip to N.
- **Windowed path**, otherwise: keep ONE `executing` set of `asyncio.Task`s across
  iterations. Each pass: reap finished entries (re-raising their exceptions), compute
  free capacity as `concurrency - len(executing)`, and claim
  `min(effective_batch_size, capacity)` when capacity allows. On an empty claim, wait
  for progress with
  `asyncio.wait(executing, return_when=FIRST_COMPLETED, timeout=poll_interval)`, or
  sleep `poll_interval` when nothing is in flight.
- Dispatch each claimed task through `client._execute_task(claimed, claim_timeout)` —
  the counted path `adrain_queue` already uses. Its existing `noqa: SLF001` comment
  explains why; the new call site needs the same, and that is a pre-approved ignore, not
  a new one.
- Head the loop with a comment naming its origin and its exit condition: ported from
  `https://github.com/earendil-works/absurd/pull/137`, to be deleted in favour of
  `client.start_worker` once a released SDK carries that fix. Full URL, not a bare
  `#137` — repo convention for issue references in code.
- `finally`: `await` the window so a graceful stop lets in-flight work finish. Task 2
  adds the cancellation half.

- [ ] **Step 6: Update the existing stop test**

`tests/core/test_worker.py:446` (`test_blocking_worker_drains_then_stops`) drives
shutdown through `client.stop_worker()`, which nothing reads any more — it would hang.
Give it an `asyncio.Event`, pass it as `stop=`, and have the stopper set it after
awaiting each task result. Update the comment: the flag it names no longer exists.

- [ ] **Step 7: Run the worker tests**

```bash
uv run pytest tests/core/test_worker_slot_refill.py tests/core/test_worker.py tests/core/test_async_worker.py -q --no-cov
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add tests/core/test_worker_slot_refill.py django_absurd/worker.py tests/core/test_worker.py
uv run pre-commit run --all-files
git commit -m "fix(worker): refill concurrency slots instead of joining each batch"
```

---

### Task 2: Graceful stop and cancel-and-drain

**Files:**

- Modify: `tests/core/test_worker_slot_refill.py`
- Modify: `django_absurd/worker.py` (the loop's `finally` from Task 1)

**Interfaces:**

- Consumes: `run_blocking_worker(..., *, stop=...)` from Task 1, `hold_until_released`,
  `record_started`, `arm_events`, `get_default_backend` from that test module.
- Produces: no new API. Locks two behaviors: stop → in-flight completes and nothing new
  is claimed; cancel → in-flight is cancelled and awaited, leaving no orphan task on the
  loop.

- [ ] **Step 1: Write the graceful-stop test**

Append to `tests/core/test_worker_slot_refill.py`:

```python
def test_stopping_lets_in_flight_work_finish_and_claims_nothing_new() -> None:
    call_command("absurd_sync_queues")
    arm_events("slow", "unclaimed")

    slow = hold_until_released.enqueue("slow")
    record_started.enqueue("unclaimed")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(get_default_backend(), "default") as client:

            async def stop_once_the_slow_task_holds_a_slot() -> None:
                await asyncio.wait_for(STARTED["slow"].wait(), timeout=10)
                stop.set()
                HOLD["gate"].set()

            await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=1), stop=stop),
                stop_once_the_slow_task_holds_a_slot(),
            )

    asyncio.run(drive())

    assert ORDER == ["slow"]
    assert not STARTED["unclaimed"].is_set()
```

Note the assertion pair: the run that was already executing reached its end (`ORDER`
holds it), and the queued one was never claimed after the stop.

- [ ] **Step 2: Write the cancellation test**

```python
def test_cancelling_the_worker_leaves_no_handler_running_on_the_loop() -> None:
    call_command("absurd_sync_queues")
    arm_events("slow")

    hold_until_released.enqueue("slow")

    async def drive() -> None:
        async with aworker_client(get_default_backend(), "default") as client:
            worker = asyncio.create_task(
                run_blocking_worker(client, WorkerOptions(concurrency=2))
            )
            await asyncio.wait_for(STARTED["slow"].wait(), timeout=10)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            leftovers = [
                pending
                for pending in asyncio.all_tasks()
                if pending is not asyncio.current_task() and not pending.done()
            ]
            assert leftovers == []

    asyncio.run(drive())
```

`aworker_client` closes the connection on exit; a handler still running there is exactly
the failure this catches — the assertion runs INSIDE the context manager so a leftover
handler is visible before the close.

- [ ] **Step 3: Run both, expect the cancellation one to fail**

```bash
uv run pytest tests/core/test_worker_slot_refill.py -q --no-cov
```

Expected: `test_stopping_lets_in_flight_work_finish_and_claims_nothing_new` passes (Task
1's `finally` already awaits the window);
`test_cancelling_the_worker_leaves_no_handler_running_on_the_loop` FAILS with a
non-empty `leftovers` list — `asyncio.wait` neither cancels nor awaits what it waits on.

- [ ] **Step 4: Implement cancel-and-drain**

Extend the loop's `finally` in `django_absurd/worker.py`: on `asyncio.CancelledError`,
cancel every task still in the window and await them (swallowing their own
`CancelledError`) before re-raising. The graceful path — stop set, no cancellation —
keeps awaiting them to completion untouched. The sync worker gets this free from
`ThreadPoolExecutor.__exit__`; the async loop has to do it by hand.

- [ ] **Step 5: Run the file again**

```bash
uv run pytest tests/core/test_worker_slot_refill.py -q --no-cov
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/core/test_worker_slot_refill.py django_absurd/worker.py
uv run pre-commit run --all-files
git commit -m "fix(worker): cancel and drain in-flight runs when the worker is cancelled"
```

---

### Task 3: Move execution-only tests onto `dj_absurd.drain()`

Mechanical, and `--burst` still exists throughout — the suite stays green at every step.
These tests only need their tasks executed; they assert nothing about the command.

**Files:**

- Modify: `tests/utils.py:68` (`run_absurd_worker`)
- Modify: `tests/core/test_admin/test_run.py:39,41,105`,
  `tests/core/test_admin/test_task.py:228,257,332`,
  `tests/core/test_admin/utils.py:41,42,51`, `tests/core/test_admin_views.py:28,29`,
  `tests/core/test_cleanup.py:59`, `tests/core/test_enqueue.py:115,148`,
  `tests/core/test_orm_models.py:71,72`,
  `tests/core/test_scheduler.py:191,221,281,316,339,365,389,391,421,423,544,795`

**Interfaces:**

- Consumes: `dj_absurd.drain(queue)` (`AbsurdTestRuntime.drain`, already public) and
  `worker.drain_queue(queue)` for non-test-function helpers that have no fixture.
- Produces: `run_absurd_worker(queue="default")` keeps its name and drops `concurrency`
  — nothing that survives this task passes it.

- [ ] **Step 1: Move the async-overlap proof onto the blocking worker first**

`tests/core/test_async_worker.py:122` (`test_async_concurrency_is_not_serial`) is the
only caller that passes `concurrency` to the helper, and it is the suite's only proof
that async tasks overlap at all. It has to move before the helper changes: at
`concurrency=1` its 1.5s bound cannot hold (4 × 0.5s serial is 2.0s).

Rewrite it against `run_blocking_worker` with a `stop=` Event: four
`atasks.asleeper(0.5)` enqueues, worker at `concurrency=4`, stop once all four results
are terminal, assert the wall clock stays under 1.5s. Keep the existing comment's
arithmetic — it explains the threshold. Drop the stale
`# burst now drains CONCURRENTLY (gather)` comment.

```bash
uv run pytest tests/core/test_async_worker.py -q --no-cov
```

Expected: PASS, with `--burst` still present and untouched.

- [ ] **Step 2: Repoint the shared helper**

`tests/utils.py:68` currently calls
`call_command("absurd_worker", queue=queue, burst=True, concurrency=concurrency)`. Make
it call `django_absurd.worker.drain_queue(queue)` and drop the `concurrency` parameter —
after Step 1 nothing passes it. Keep the function name; 34 call sites read
`run_absurd_worker()` today.

Then run only what reaches the helper:

```bash
uv run pytest tests/core/test_absurd_fixture.py tests/core/test_async_worker.py tests/core/test_worker.py -q --no-cov
```

Expected: all pass.

- [ ] **Step 3: Convert the direct call sites in test modules that already request
      `dj_absurd`**

For each site listed under Files, replace
`call_command("absurd_worker", queue=<q>, burst=True)` with `dj_absurd.drain(<q>)` where
the test already takes the `dj_absurd` fixture, adding the fixture parameter where it
does not (alphabetized among the test's other fixture parameters).

Every one of these tests is already `transaction=True` — a burst worker on its own
connection could only ever see committed rows — so no marker changes are needed. If any
test turns out not to be, that is a finding: report it rather than adding the marker
blind.

- [ ] **Step 4: Convert the module-level helpers**

`tests/core/test_admin/utils.py:41,42,51`, `tests/core/test_admin_views.py:28,29` and
`tests/core/test_orm_models.py:71,72` call the command from plain helper functions with
no fixture in scope. Those call `worker.drain_queue(<queue>)` directly — the same
entrypoint `dj_absurd.drain` wraps.

- [ ] **Step 5: Run the modules this task touched**

```bash
uv run pytest tests/core/test_admin tests/core/test_admin_views.py \
  tests/core/test_cleanup.py tests/core/test_enqueue.py \
  tests/core/test_orm_models.py tests/core/test_scheduler.py \
  tests/core/test_absurd_fixture.py tests/core/test_async_worker.py \
  tests/core/test_worker.py -q --no-cov
```

Expected: all pass. Nothing else in the repo calls the worker command, so no whole-suite
run here.

- [ ] **Step 6: Commit**

```bash
git add tests/
uv run pre-commit run --all-files
git commit -m "test: drain through the fixture instead of the worker command"
```

---

### Task 4: Live-worker helper for command-level assertions

Command-test parity is a requirement: every assertion that runs the command today keeps
running the command. With `--burst` gone the command's only stop is a signal, so this
task builds the one helper that owns that shape and moves the command-level tests onto
it — while `--burst` still exists, so each step is verifiable.

**Files:**

- Modify: `tests/utils.py` (new helper, placed below `run_absurd_worker`)
- Modify: `tests/core/test_command_output.py:27,40,54`,
  `tests/core/test_logging/test_handler.py:40,52`,
  `tests/core/test_orm_views.py:95,110,112`,
  `tests/core/test_worker.py:178,209,264,274,304,337,359,386`

**Interfaces:**

- Consumes: `call_command("absurd_worker", **options)`, the `django_absurd.worker`
  logger, `signal.getsignal`.
- Produces:
  `run_worker_command_until(is_done: t.Callable[[], bool] | None = None, *, timeout: float = 15.0, **options: t.Any) -> None`
  — runs the command in the calling thread and stops it with `SIGTERM` once `is_done()`
  holds (default: as soon as the worker's loop is running).

- [ ] **Step 1: Write the helper**

Add to `tests/utils.py`, below `run_absurd_worker`. The module already imports
`threading`, `typing as t` and `django.db.connections`; add `os`, `signal` and `time`.

```python
def run_worker_command_until(
    is_done: "t.Callable[[], bool] | None" = None,
    *,
    timeout: float = 15.0,
    **options: t.Any,
) -> None:
    """Run ``absurd_worker`` to completion, stopping it once ``is_done()`` holds.

    The command runs in the calling thread so ``capsys``/``caplog`` see it; a watcher
    thread fires the SIGTERM the worker's own signal handler turns into a graceful
    stop. Waits for that handler to be installed first — a signal delivered before
    then kills the test session instead.
    """
    previous_handler = signal.getsignal(signal.SIGTERM)

    def stop_once_done() -> None:
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if signal.getsignal(signal.SIGTERM) is not previous_handler and (
                    is_done is None or is_done()
                ):
                    break
                time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)
        finally:
            connections.close_all()

    watcher = threading.Thread(target=stop_once_done, daemon=True)
    watcher.start()
    try:
        call_command("absurd_worker", **options)
    finally:
        watcher.join(timeout=5)
```

`connections.close_all()` is thread-local, so it closes only the watcher's own
connection — the predicate may query the ORM from that thread.

- [ ] **Step 2: Convert the simplest command test and run it**

`tests/core/test_command_output.py:27` asserts the banner. Replace
`call_command("absurd_worker", queue="default", burst=True, stdout=out)` with
`utils.run_worker_command_until(queue="default", stdout=out)` — no predicate: the banner
is printed before the loop starts.

```bash
uv run pytest tests/core/test_command_output.py -q --no-cov
```

Expected: PASS, and in well under the 15s timeout. If it hangs to the timeout, the
handler-installed check is not seeing the worker's handler — stop and report rather than
lowering the timeout.

- [ ] **Step 3: Convert the remaining output/logging tests**

Same substitution, no predicate, for `test_command_output.py:40,54` and
`tests/core/test_logging/test_handler.py:40,52` (both assert what handler configuration
the command left behind, not what ran).

- [ ] **Step 4: Convert the tests that need work to have finished**

These assert on rows the worker produced, so they pass a predicate:

- `tests/core/test_worker.py:178` (`test_queue_defaults_to_default`) — predicate
  `lambda: Group.objects.filter(name="dflt").exists()`. Its stdout assertion (exact
  `🐘 Started` / `🐘 Stopped` pair) stays as-is: a graceful stop still prints the stop
  line.
- `tests/core/test_worker.py:264` (`test_command_burst_runs_task_end_to_end`) — same
  shape, predicate on `Group.objects.filter(name="via-command").exists()`. Rename it
  `test_command_runs_task_end_to_end`; "burst" is not a concept any more.
- `tests/core/test_orm_views.py:95,110,112` — predicate on the admin model's rows for
  the queue under test (`task_model.objects.filter(queue="other").exists()`).

- [ ] **Step 5: Convert the provisioning and reconcile tests**

`tests/core/test_worker.py:209,274,304,337,359,386` assert the sync report the command
prints before the loop starts, so they need no predicate — the default (stop as soon as
the loop is running) is right. `test_worker_command_warns_on_storage_mode_drift`
additionally asserts the `worker started: … burst=True concurrency=1` log line at
`test_worker.py:399`; leave that assertion untouched here, Task 5 changes the line
itself.

- [ ] **Step 6: Run the modules this task touched**

```bash
uv run pytest tests/core/test_command_output.py tests/core/test_logging \
  tests/core/test_orm_views.py tests/core/test_worker.py -q --no-cov
```

Expected: all pass, none of them near the helper's 15s timeout.

- [ ] **Step 7: Commit**

```bash
git add tests/
uv run pre-commit run --all-files
git commit -m "test: stop the worker command with a signal instead of burst"
```

---

### Task 5: Remove `--burst`

Nothing depends on it now except the tests that exist to describe it.

**Files:**

- Modify: `django_absurd/management/commands/absurd_worker.py:30-34,82-84,114-120`
- Modify: `django_absurd/worker.py:139-160` (`drain_queue`), `163-218`
  (`run_worker`/`arun_worker`), `66` and `90` (docstrings)
- Modify: `django_absurd/test.py:457,531,753,755`, `django_absurd/pytest_plugin.py:117`
  (prose)
- Modify: `tests/core/test_worker.py:64,197,224,241,409,483` (drop the kwarg),
  `tests/core/test_worker.py:399` and `tests/core/test_logging/test_maintenance.py:58`
  (log-line assertions)
- Delete: `tests/core/test_scheduler.py:663-671` (`test_worker_beat_rejects_burst`)
- Modify: `tests/core/test_async_worker.py:122-128`
  (`test_async_concurrency_is_not_serial`)

**Interfaces:**

- Consumes: `run_blocking_worker(..., *, stop=...)` (Task 1), `run_worker_command_until`
  (Task 4).
- Produces:
  `run_worker(backend: AbsurdBackend, queue: str, *, run_beat: bool = False, options: WorkerOptions | None = None) -> None`
  — no `burst` parameter, no return value.
  `drain_queue(queue: str = "default") -> list[DrainedRun]` keeps its signature and
  behavior.

- [ ] **Step 1: Drop the flag from the command**

In `django_absurd/management/commands/absurd_worker.py`: delete the `--burst` argument,
the `--beat`/`--burst` conflict check and its `CommandError`, and the `burst=` argument
in the `run_worker(...)` call.

Delete `tests/core/test_scheduler.py:663-671` (`test_worker_beat_rejects_burst`) — it
asserts the conflict message that no longer exists. Drop the now-unused `burst=True`
kwarg from `tests/core/test_worker.py:64,197,224,241,409,483`; each of those raises
before the worker loop, so they keep passing unchanged otherwise.

- [ ] **Step 2: Drop the parameter from the worker module**

- `run_worker` loses `burst`; it always runs the blocking worker (with or without beat)
  and returns `None`.
- `drain_queue` stops going through `run_worker`. It keeps its current guards
  (`QueueNotDeclaredError` for an undeclared queue, the `UndefinedTable` →
  `QueueNotProvisionedError` translation via `names_a_queue_table`) and runs the drain
  through the same executor + `aworker_client` plumbing `arun_worker` uses, at
  `concurrency=1`. `adrain_queue` itself is unchanged.
- `arun_worker` loses its `burst` branch. Whatever plumbing both entrypoints still share
  (thread pool sized to concurrency, client context, the started/stopped log pair) stays
  in one place; do not duplicate it.

- [ ] **Step 3: Fix the log line and the prose**

- `worker.py:191` — drop ` burst=%s` and its argument from the `worker started:` format.
- Update the two assertions that spell that line out verbatim:
  `tests/core/test_worker.py:399` and `tests/core/test_logging/test_maintenance.py:58`.
- Replace "burst drain"/"burst worker" wording with "drain" in `worker.py:66,90,328`,
  `django_absurd/test.py:457,531,753,755`, `django_absurd/pytest_plugin.py:117`.

- [ ] **Step 4: Prove the flag is gone**

```bash
uv run pytest tests/core/test_worker.py tests/core/test_worker_slot_refill.py \
  tests/core/test_async_worker.py tests/core/test_scheduler.py \
  tests/core/test_logging tests/core/test_command_output.py -q --no-cov
grep -rn "burst" django_absurd/ tests/
```

Expected: those modules pass; `grep` returns nothing outside `docs/` (specs and plans
keep their historical references). A hit in `django_absurd/AGENTS.md` is Task 6's job.
The whole-suite check for this signature change is the coordinator's gate, not yours.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/ tests/
uv run pre-commit run --all-files
git commit -m "feat!: remove absurd_worker --burst"
```

---

### Task 6: Documentation

**Files:**

- Modify: `django_absurd/AGENTS.md:249-267` (Workers section), `:852` (drain prose)
- Modify: `docs/web/testing.md:122`
- Modify: `tests/CLAUDE.md:40` (the freezegun note says "burst drain")

**Interfaces:**

- Consumes: the shipped behavior from Tasks 1-5.
- Produces: no code.

- [ ] **Step 1: Rewrite the Workers section**

`django_absurd/AGENTS.md:249-267`: one worker mode. Delete the Blocking/Burst bullet
pair and the `--burst` mention, including its "cron / one-shot" claim — that advertised
test scaffolding as a deployment mode and was wrong when written. Describe
`--concurrency N` as the number of tasks in flight, refilled as soon as a slot frees,
and say a stop signal (`SIGINT`/`SIGTERM`) lets in-flight tasks finish before the worker
exits.

- [ ] **Step 2: Describe the drain on its own terms**

`django_absurd/AGENTS.md:852` and `docs/web/testing.md:122` both explain
`dj_absurd.drain()` as "the fixture counterpart of `absurd_worker --burst`". Say what it
does instead: runs every currently-claimable task on the queue to completion,
in-process, one at a time, returning one `RunSnapshot` per run in claim order.

- [ ] **Step 3: Fix the test-conventions note**

`tests/CLAUDE.md:40` warns that freezegun deadlocks "the burst drain". Same warning,
current vocabulary: "the drain".

- [ ] **Step 4: Verify no stale references**

```bash
grep -rn --hidden "burst" django_absurd/ docs/web/ tests/ README.md examples/
```

Expected: no hits. `--hidden` matters — a bare `ag`/`grep` sweep in this repo has missed
stale references before.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/testing.md tests/CLAUDE.md
uv run pre-commit run --all-files
git commit -m "docs: one worker mode, with concurrency that refills"
```

---

## Coordinator gate (after Task 6)

Not a task — the coordinator runs these once, after the last commit:

```bash
docker compose up -d db db_pg_cron
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
```

Then a `revdiff` pass against `origin/main` before any push, per the project's review
flow. The branch stays local until then.
