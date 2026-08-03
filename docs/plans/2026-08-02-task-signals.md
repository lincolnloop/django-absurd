# Task Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `AbsurdBackend` sends Django's `task_enqueued` / `task_started` /
`task_finished`, so Django's own `django.tasks` logging works for Absurd tasks.

**Architecture:** Two seams. `backends.enqueue` sends `task_enqueued` after a successful
spawn. `worker.build_handler` sends `task_started` per handler entry, and
`task_finished` on a success or on a terminal failure the task's own code raised —
inline, where the handler already is. Endings Absurd decides on its own send nothing.
Every send goes through one contained helper in a new `django_absurd/dispatch.py`, so a
receiver can never fail a task. The signals are not an extension point: users are not
invited to build on them.

**Tech Stack:** Django 6.0 Tasks API, absurd-sdk, psycopg3, pytest + pytest-django +
pytest-asyncio, the project's `dj_absurd` fixture.

**Spec:**
[`docs/specs/2026-08-01-task-signals-design.md`](../specs/2026-08-01-task-signals-design.md).
Read it before Task 1 — it carries the reasoning this plan only executes.

## Global Constraints

- Floor Django 6.0 / Python 3.12. psycopg3 backend only.
- `import typing as t` — never `from typing import X`. Absolute imports only.
- Functions contain a verb. No leading-underscore module constants or helpers. Helpers
  go BELOW the public function that uses them.
- Own exception types under `DjangoAbsurdError` in `django_absurd/exceptions.py`; the
  exception owns its message; constructor takes the data.
- Re-raise inside `except` always chains `from exc`. Never `from None`.
- No ruff ignores or `noqa` beyond the one **pre-authorized in Task 5**. No
  `unittest.mock.patch`. No comments narrating prior state.
- Log past tense, after the action.
- 100% statement + branch coverage on lines this patch adds.
- Tests: pytest, function-based only. Read [`tests/CLAUDE.md`](../../tests/CLAUDE.md)
  first.
  - Autouse `_enable_db` gives DB access. Add `@pytest.mark.django_db(transaction=True)`
    to any test that drains, crosses a thread, or calls `dj_absurd.get_result` —
    `guard_against_open_transaction` raises otherwise.
  - Type the fixture: `dj_absurd: AbsurdTestRuntime`, from `django_absurd.test`. Never
    `t.Any` — the real type is what catches the two-result-types mistake below
    statically.
  - Fixture parameters are alphabetized: `(caplog, dj_absurd)`.
  - Anything that executes freezes time through `dj_absurd`, never `time.sleep`.

**The two result types — the single most likely mistake in this plan:**

| Call                                      | Returns             | Has                                                                |
| ----------------------------------------- | ------------------- | ------------------------------------------------------------------ |
| `task_backends["default"].get_result(id)` | Django `TaskResult` | `.status`, `.return_value`, `.errors`, `.attempts`                 |
| `dj_absurd.get_result(id)`                | `TaskSnapshot`      | `.queue`, `.task_id`, `.state`, `.result`, `.failure`, `.attempts` |

`TaskSnapshot` has **no** `.id`, `.status`, `.return_value` or `.errors`. Use the
backend for Django semantics, the fixture for Absurd state.
`test_run_after.py:37,46-48,71-72` uses both side by side.

- Gates: `uv run pytest tests/core/<file> -v` while iterating, then
  `uvx --with tox-uv tox -e dev` and `uv run pre-commit run --all-files` before each
  task's final commit. Never invoke ruff or mypy directly.
- Compose services up first: `docker compose up -d db db_pg_cron`.

---

### Task 1: Contained dispatch + the enqueue seam

**Files:**

- Create: `django_absurd/dispatch.py`
- Create: `tests/core/test_signals_enqueue.py`
- Modify: `django_absurd/backends.py` (`enqueue`, at the `TaskResult` construction,
  `backends.py:206-219`)
- Modify: `tests/utils.py`

**Interfaces:**

- Consumes: nothing.
- Produces:
  - `django_absurd.dispatch.send_task_signal(signal: Signal, sender: type, task_result: TaskResult[t.Any, t.Any]) -> None`
    — sends, catching `Exception` (never `BaseException`), logging failures. Tasks 3-6
    use it.
  - `tests.utils.connect_receiver(signal, receiver, *, sender)` — `contextmanager`.
  - `tests.utils.RecordingReceiver` — thread-safe collector; `.results` returns a copy.

- [ ] **Step 1: Add the test-support helpers**

Append to `tests/utils.py`, merging imports into its existing block:

```python
class RecordingReceiver:
    """Collects TaskResults from a signal.

    Thread-safe because the enqueue seam sends on whatever thread called it, so a sync
    task body enqueueing at concurrency>1 reaches it from several pool threads at once
    and a bare list would race. A class rather than a closure so a test can hold one
    object and read it after the block.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.received: list[TaskResult[t.Any, t.Any]] = []

    def __call__(
        self, sender: type, task_result: "TaskResult[t.Any, t.Any]", **kwargs: t.Any
    ) -> None:
        with self.lock:
            self.received.append(task_result)

    @property
    def results(self) -> list["TaskResult[t.Any, t.Any]"]:
        with self.lock:
            return list(self.received)


@contextlib.contextmanager
def connect_receiver(
    signal: Signal, receiver: t.Any, *, sender: type
) -> t.Iterator[None]:
    """Connect for the duration of the block, always disconnecting.

    connect() sits inside the try so a failure anywhere after it still disconnects; a
    receiver leaked here fires for every later test in the same process. weak=False
    because Signal.connect otherwise holds a weak reference and a receiver the caller
    does not keep alive can be collected mid-test, silently never firing.
    """
    try:
        signal.connect(receiver, sender=sender, weak=False)
        yield
    finally:
        signal.disconnect(receiver, sender=sender)
```

Needs `contextlib`, `threading`, `django.dispatch.Signal`, `django.tasks.TaskResult` in
the module's imports.

- [ ] **Step 2: Write the failing tests**

`tests/core/test_signals_enqueue.py`, complete and copy-pasteable:

```python
import asyncio
import datetime as dt
import logging
import typing as t

import pytest
from django.tasks import TaskResultStatus
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone

from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import models, tasks, utils


def test_enqueue_sends_task_enqueued() -> None:
    receiver = utils.RecordingReceiver()
    with utils.connect_receiver(task_enqueued, receiver, sender=AbsurdBackend):
        result = tasks.add.enqueue(1, 2)

    assert [r.id for r in receiver.results] == [result.id]
    assert receiver.results[0].status == TaskResultStatus.READY
    assert receiver.results[0].task.module_path == tasks.add.module_path


@pytest.mark.django_db(transaction=True)
def test_enqueued_payload_id_round_trips(dj_absurd: AbsurdTestRuntime) -> None:
    receiver = utils.RecordingReceiver()
    with utils.connect_receiver(task_enqueued, receiver, sender=AbsurdBackend):
        tasks.add.enqueue(1, 2)

    sent_id = receiver.results[0].id
    snapshot = dj_absurd.get_result(sent_id)
    assert f"{snapshot.queue}:{snapshot.task_id}" == sent_id


@pytest.mark.django_db(transaction=True)
def test_aenqueue_sends_one_task_enqueued() -> None:
    receiver = utils.RecordingReceiver()
    with utils.connect_receiver(task_enqueued, receiver, sender=AbsurdBackend):
        result = asyncio.run(tasks.add.aenqueue(1, 2))

    assert [r.id for r in receiver.results] == [result.id]


@pytest.mark.django_db(transaction=True)
def test_an_aenqueue_receiver_can_write_to_the_database() -> None:
    """Django hops aenqueue off the loop itself via
    sync_to_async(thread_sensitive=True), so this is the one seam where a receiver can
    reach the ORM. Asserted rather than assumed.
    """

    def write_a_row(sender: type, task_result: t.Any, **kwargs: t.Any) -> None:
        models.Payload.objects.create(data={"id": task_result.id})

    with utils.connect_receiver(task_enqueued, write_a_row, sender=AbsurdBackend):
        asyncio.run(tasks.add.aenqueue(1, 2))

    assert models.Payload.objects.count() == 1


def test_enqueue_survives_a_receiver_that_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(sender: type, task_result: t.Any, **kwargs: t.Any) -> None:
        raise RuntimeError("receiver is broken")

    with utils.connect_receiver(task_enqueued, explode, sender=AbsurdBackend):
        with caplog.at_level(logging.ERROR, logger="django_absurd"):
            result = tasks.add.enqueue(1, 2)

    assert result.status == TaskResultStatus.READY
    errors = [r for r in caplog.records if r.name == "django_absurd"]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


def test_django_logs_the_enqueue(caplog: pytest.LogCaptureFixture) -> None:
    # tests/settings.py sets no LOGGING, so Django's default puts "django" at INFO and
    # this DEBUG record is dropped at the logger before any handler sees it.
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    result = tasks.add.enqueue(1, 2)

    records = [r for r in caplog.records if r.name == "django.tasks"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert result.id in records[0].getMessage()
    assert tasks.add.module_path in records[0].getMessage()


def test_absurd_sender_filter_ignores_another_backend() -> None:
    receiver = utils.RecordingReceiver()
    on_immediate = tasks.add.using(backend="immediate")
    with utils.connect_receiver(task_enqueued, receiver, sender=AbsurdBackend):
        on_immediate.enqueue(1, 2)

    assert receiver.results == []


@pytest.mark.django_db(transaction=True)
def test_a_deferred_enqueue_sends_two_signals(dj_absurd: AbsurdTestRuntime) -> None:
    enqueued = utils.RecordingReceiver()
    started = utils.RecordingReceiver()
    finished = utils.RecordingReceiver()
    with utils.connect_receiver(task_enqueued, enqueued, sender=AbsurdBackend):
        with utils.connect_receiver(task_started, started, sender=AbsurdBackend):
            with utils.connect_receiver(task_finished, finished, sender=AbsurdBackend):
                with dj_absurd.freeze_time() as frozen_time:
                    due = timezone.now() + dt.timedelta(hours=1)
                    wrapper = tasks.add.using(run_after=due).enqueue(1, 2)
                    assert [r.id for r in enqueued.results] == [wrapper.id]

                    frozen_time.shift(dt.timedelta(hours=2))
                    dj_absurd.drain()

    sent = enqueued.results
    assert len(sent) == 2
    assert sent[0].task.run_after is not None
    assert sent[1].task.run_after is None
    assert sent[0].id != sent[1].id
    # The wrapper's id is a permanent orphan: the wrapper handler sends nothing, so only
    # the real task's id ever starts or finishes. A refactor breaks this silently.
    # Both assertions are vacuous until Task 3 sends task_started; Task 3 adds the
    # positive half that makes them load-bearing.
    assert wrapper.id not in [r.id for r in started.results]
    assert wrapper.id not in [r.id for r in finished.results]
```

`run_after` must be a **datetime**: `validate_task` calls `timezone.is_aware()` on it,
so a `timedelta` raises `AttributeError` at `.using()` time. Precedent:
`test_run_after.py:116-117`.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/core/test_signals_enqueue.py -v` Expected: every test FAILS on
empty `receiver.results` or zero `django.tasks` records, EXCEPT
`test_absurd_sender_filter_ignores_another_backend`, which asserts emptiness and passes
at RED — it is a guard against over-sending later, not a RED target.

- [ ] **Step 4: Write `django_absurd/dispatch.py`**

Minimal, in prose:

- Module docstring: the one place every signal send goes through, so a receiver's
  exception can never reach the task.
- `logger = logging.getLogger("django_absurd")` — the flat name the package uses today;
  the hierarchy rename belongs to a later unit of #25.
- `send_task_signal(signal, sender, task_result)`:
  `signal.send(sender, task_result=...)` inside `try`; `except Exception:` →
  `logger.exception(...)` naming the task result id. `Exception`, not `BaseException` —
  `asyncio.CancelledError` at shutdown must propagate.
- One function. Every seam — enqueue and both worker sends — calls it directly.

- [ ] **Step 5: Send from `enqueue`**

Bind the constructed `TaskResult` to a name instead of returning it inline, call
`dispatch.send_task_signal(task_enqueued, type(self), result)`, then return it. Import
`from django_absurd import dispatch` and the signal from `django.tasks.signals`.
Placement: after the spawn succeeded, before the return — never before the spawn.

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/core/test_signals_enqueue.py -v` Expected: PASS, all nine. The
deferred test passes with no extra production change — the wrapper's inner enqueue
already goes through this seam.

- [ ] **Step 7: Full gates, then commit**

```bash
docker compose up -d db db_pg_cron
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/dispatch.py django_absurd/backends.py tests/utils.py tests/core/test_signals_enqueue.py
git commit -m "feat: send task_enqueued from AbsurdBackend.enqueue"
```

---

### Task 2: Trust the worker's backend and queue, not the task

**Files:**

- Modify: `django_absurd/worker.py` (`LazyTaskRegistry`, `build_handler`,
  `build_task_context`, the registry construction at `worker.py:215`)
- Create: `tests/core/test_signals_worker_result.py`
- Modify: `tests/tasks.py` (one fixture task)

**Interfaces:**

- Consumes: nothing from Task 1.
- Produces: `build_handler(task, *, backend: AbsurdBackend, queue: str)`,
  `build_task_context(task, ctx, args, kwargs, *, backend, queue)`,
  `LazyTaskRegistry(queue, backend)`. Tasks 3-6 send with `sender=type(backend)`.

No signals in this task. Deliverable: the worker-side `TaskResult` is correct, which
every later payload depends on.

- [ ] **Step 1: Add the fixture task**

In `tests/tasks.py`, matching the file's existing style (`JsonValue` is already imported
there, and `scoverage` shows the return idiom):

```python
@task(takes_context=True)
def report_context(context: "TaskContext[t.Any, t.Any]") -> dict[str, JsonValue]:
    return {
        "id": context.task_result.id,
        "backend": context.task_result.backend,
        "attempts": context.task_result.attempts,
    }
```

`dict[str, JsonValue]`, not `dict[str, str]` — `attempts` is an `int`, and mypy strict
runs on tests.

- [ ] **Step 2: Write the failing tests**

`tests/core/test_signals_worker_result.py`:

```python
import pytest

from django_absurd.test import AbsurdTestRuntime
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def test_worker_result_id_is_queue_prefixed(dj_absurd: AbsurdTestRuntime) -> None:
    result = tasks.report_context.enqueue()
    dj_absurd.drain()

    payload = dj_absurd.get_result(result.id).result
    assert payload["id"] == result.id
    assert payload["id"].startswith("default:")


def test_worker_result_id_round_trips(dj_absurd: AbsurdTestRuntime) -> None:
    result = tasks.report_context.enqueue()
    dj_absurd.drain()

    payload = dj_absurd.get_result(result.id).result
    snapshot = dj_absurd.get_result(payload["id"])
    assert f"{snapshot.queue}:{snapshot.task_id}" == result.id


def test_worker_result_names_the_absurd_backend(dj_absurd: AbsurdTestRuntime) -> None:
    result = tasks.report_context.enqueue()
    dj_absurd.drain()

    assert dj_absurd.get_result(result.id).result["backend"] == "default"
```

`dj_absurd.get_result(...).result` — the snapshot's payload field. Not `.return_value`,
which `TaskSnapshot` does not have. Read `tests/core/test_worker.py:111-129` for the
`takes_context` drain idiom.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/core/test_signals_worker_result.py -v` Expected: FAIL, and not
for the obvious reason — today the id is a `uuid.UUID`, so the SDK's `json.dumps` of the
return payload raises `TypeError`, the task burns its attempts, and `.result` is `None`.
The failure surfaces as `TypeError: 'NoneType' object is not subscriptable`. That IS the
RED; do not go hunting for a different cause.

- [ ] **Step 4: Plumb the backend and queue through**

- `LazyTaskRegistry.__init__` takes the resolved `AbsurdBackend` alongside `queue`,
  stores both, passes both to `build_handler`.
- `aworker_client` already has `backend` — pass it at the construction on
  `worker.py:215`.
- `build_handler(task, *, backend, queue)` forwards both to `build_task_context`.
- `build_task_context` builds `id=f"{queue}:{ctx.task_id}"` — the f-string also coerces
  the `uuid.UUID` — and sets `backend=backend.alias`. Rebind with
  `.using(queue_name=queue)` when `task.queue_name` disagrees, as
  `backends.build_task_result` already does (`backends.py:334-335`).
- Comment the queue source: `ClaimedTask` carries no queue and the re-imported task
  holds its definition-time one, so only the worker knows the truth.

Leave the `TaskResult` conditional on `task.takes_context` for now — Task 3 makes it
unconditional with the first send.

- [ ] **Step 5: Run to verify pass**

Run:
`uv run pytest tests/core/test_signals_worker_result.py tests/core/test_worker.py -v`
Expected: PASS, including the pre-existing `takes_context` tests.

Coverage note: the `.using(queue_name=queue)` rebind's True arm is NOT reachable in this
task — only `takes_context` tasks build the result here, and none is drained off-queue.
It becomes covered in Task 3, when the build goes unconditional and `test_worker.py`'s
off-queue routing test drains through it. Do not chase it now.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/worker.py tests/tasks.py tests/core/test_signals_worker_result.py
git commit -m "fix: build the worker-side TaskResult id from the worker's own queue"
```

---

### Task 3: task_started

**Files:**

- Modify: `django_absurd/dispatch.py`
- Modify: `django_absurd/worker.py` (`build_handler`)
- Create: `tests/core/test_signals_started.py`

**Interfaces:**

- Consumes: `dispatch.send_task_signal` (Task 1);
  `build_handler(task, *, backend, queue)` (Task 2).
- Produces: nothing new. The worker calls the same contained helper the enqueue seam
  does.

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt
import logging
import typing as t

import pytest
from django.tasks import TaskResultStatus
from django.tasks.signals import task_started

from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_started_fires_once_per_successful_run(dj_absurd: AbsurdTestRuntime) -> None:
    receiver = utils.RecordingReceiver()
    with utils.connect_receiver(task_started, receiver, sender=AbsurdBackend):
        with dj_absurd.freeze_time():
            result = tasks.add.enqueue(1, 2)
            dj_absurd.drain()

    assert [r.id for r in receiver.results] == [result.id]
    assert receiver.results[0].status == TaskResultStatus.RUNNING
    assert receiver.results[0].attempts == 1
    assert receiver.results[0].started_at is not None
    assert receiver.results[0].enqueued_at is None


def test_started_fires_per_handler_entry_across_a_sleep(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    receiver = utils.RecordingReceiver()
    with utils.connect_receiver(task_started, receiver, sender=AbsurdBackend):
        with dj_absurd.freeze_time() as frozen_time:
            result = tasks.sleep_a_week.enqueue()
            dj_absurd.drain()          # entry 1: suspends on the durable sleep
            assert len(receiver.results) == 1

            frozen_time.shift(dt.timedelta(days=8))
            dj_absurd.drain()          # entry 2: replays and finishes

    assert len(receiver.results) == 2
    # One attempt, two entries: the run is rescheduled, never re-created.
    assert [r.attempts for r in receiver.results] == [1, 1]
    assert dj_absurd.get_result(result.id).state == "completed"


def test_a_raising_receiver_does_not_fail_the_task(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """Uncontained, this exception escapes the handler and the SDK reads it as the TASK
    failing — an audit receiver would burn every attempt of every task.
    """

    def explode(sender: type, task_result: t.Any, **kwargs: t.Any) -> None:
        raise RuntimeError("receiver is broken")

    with utils.connect_receiver(task_started, explode, sender=AbsurdBackend):
        with caplog.at_level(logging.ERROR, logger="django_absurd"):
            with dj_absurd.freeze_time():
                result = tasks.add.enqueue(1, 2)
                dj_absurd.drain()

    assert dj_absurd.get_result(result.id).state == "completed"
    contained = [
        r
        for r in caplog.records
        if r.name == "django_absurd" and r.levelno >= logging.ERROR
    ]
    assert len(contained) == 1
```

`tasks.sleep_a_week` (`tests/tasks.py:180-183`) already exists.
`test_durable.py:113-129` shows the drain-shift-drain sequence.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_signals_started.py -v` Expected: FAIL — nothing is
sent, so the recording tests see empty lists. The raising-receiver test PASSES at this
stage (the receiver never fires, so the task trivially completes and nothing is logged)
— it is a guard, not a RED target.

- [ ] **Step 3: Send from the handler**

In `build_handler`: build the `TaskResult` unconditionally (currently only under
`if task.takes_context`), reuse it for `TaskContext`, and call
`dispatch.send_task_signal(task_started, type(backend), task_result)` in the coroutine
before the body runs.

- [ ] **Step 4: Name the signal in the containment log**

Carried over from Task 1's review as a Minor, actionable here because this task already
edits `dispatch.py`. The contained-failure log names the task id but not which signal
failed, and from this task on three signals share the helper. Django `Signal` objects
are anonymous — their repr carries nothing — so the traceback is otherwise the only
clue.

Add a module-level mapping from the three `django.tasks.signals` objects to their names
and use it in the `logger.exception` message. Call sites stay unchanged. Then extend
Task 1's `test_enqueue_survives_a_receiver_that_raises` and this task's
`test_a_raising_receiver_does_not_fail_the_task` to assert `"task_enqueued"` and
`"task_started"` respectively appear in the record's message.

While here, rewrite the deferred test's comment so it describes the code as it now is
rather than as it will be — Task 1 left it half future-tense on purpose.

- [ ] **Step 5: Run to verify pass**

Run:
`uv run pytest tests/core/test_signals_started.py tests/core/test_worker.py tests/core/test_async_worker.py -v`
Expected: PASS. The existing worker tests are included deliberately — this is the step
where the unconditional `TaskResult` and the first sends could disturb them.

- [ ] **Step 6: Make the deferred orphan assertions load-bearing**

`tests/core/test_signals_enqueue.py::test_a_deferred_enqueue_sends_two_signals` (Task 1)
asserts the wrapper's id is absent from `started` and `finished`. Those were vacuous
while nothing sent `task_started`. Now add the positive half at the end of that test, so
the absence means something:

```python
    assert sent[1].id in [r.id for r in started.results]
```

Run: `uv run pytest tests/core/test_signals_enqueue.py -v` Expected: PASS — the inner
task starts under its own id, the wrapper's never does.

- [ ] **Step 7: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/dispatch.py django_absurd/worker.py tests/core/test_signals_started.py tests/core/test_signals_enqueue.py
git commit -m "feat: send task_started from the worker"
```

---

### Task 4: task_finished on success

**Files:**

- Modify: `django_absurd/worker.py` (`build_handler`'s `else` arm)
- Create: `tests/core/test_signals_finished_success.py`

**Interfaces:**

- Consumes: `dispatch.send_task_signal(signal, sender, task_result)` (Task 1) and
  `worker.build_running_task_result`, which is what Task 3 named the function this plan
  earlier called `build_task_context`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt
import logging

import pytest
from django.tasks import TaskResultStatus, task_backends
from django.tasks.signals import task_finished

from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_finished_reports_success_and_the_return_value(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    receiver = utils.RecordingReceiver()
    with utils.connect_receiver(task_finished, receiver, sender=AbsurdBackend):
        with dj_absurd.freeze_time():
            result = tasks.add.enqueue(1, 2)
            dj_absurd.drain()

    assert [r.id for r in receiver.results] == [result.id]
    sent = receiver.results[0]
    assert sent.status == TaskResultStatus.SUCCESSFUL
    assert sent.finished_at is not None
    # An unset _return_value reads as None here — silently wrong rather than raising.
    assert sent.return_value == 3
    assert task_backends["default"].get_result(result.id).return_value == 3


def test_django_logs_the_whole_successful_lifecycle(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    with dj_absurd.freeze_time():
        tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    records = [
        (r.levelno, r.getMessage()) for r in caplog.records if r.name == "django.tasks"
    ]
    assert [level for level, _ in records] == [
        logging.DEBUG,
        logging.INFO,
        logging.INFO,
    ]
    assert "state=RUNNING" in records[1][1]
    assert "state=SUCCESSFUL" in records[2][1]


def test_django_logs_a_sleeping_task_as_two_running_lines(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    with dj_absurd.freeze_time() as frozen_time:
        tasks.sleep_a_week.enqueue()
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    messages = [r.getMessage() for r in caplog.records if r.name == "django.tasks"]
    assert len([m for m in messages if "state=RUNNING" in m]) == 2
    assert len([m for m in messages if "state=SUCCESSFUL" in m]) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_signals_finished_success.py -v` Expected: FAIL —
`receiver.results == []`, and the log assertions see one DEBUG + one INFO (started, from
Task 3) with no SUCCESSFUL line.

- [ ] **Step 3: Send on the success path**

In `build_handler`'s `else` arm, mutate the frozen `TaskResult` with
`object.__setattr__` for **all three** of `status` (`SUCCESSFUL`), `finished_at` (now)
and `_return_value` — it is a `frozen=True, slots=True` dataclass, and this is how
Django's own backend mutates it (`immediate.py:71-73`). Then call
`dispatch.send_task_signal(task_finished, ...)` before returning.

`_return_value` must be set: `TaskResult.return_value` returns it and it defaults to
`None`, so omitting it hands a receiver a wrong value rather than an error.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_signals_finished_success.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/worker.py tests/core/test_signals_finished_success.py
git commit -m "feat: send task_finished when a run succeeds"
```

---

### Task 5: task_finished on failure — terminal only

**Files:**

- Modify: `django_absurd/params.py` (widen `max_attempts` to `int | None`)
- Modify: `django_absurd/worker.py` (`build_handler`'s `except Exception` arm)
- Create: `tests/core/test_signals_finished_failure.py`
- Modify: `tests/core/test_absurd_params.py` (one params-level assertion)

**Interfaces:**

- Consumes: `dispatch.send_task_signal` (Task 1) and `worker.build_running_task_result`
  (Task 3's name for what this plan earlier called `build_task_context`).
- Produces: `read_sdk_max_attempts(ctx) -> int | None` in `worker.py`, beside
  `read_sdk_attempt`.

The send goes inside the handler's `except` arm, so `sys.exc_info()` already holds the
task's exception and Django's `log_task_finished` (`signals.py:52-53`) attaches the
right traceback with nothing arranged.

**Pre-authorized:** `read_sdk_max_attempts` reads `ctx._task["max_attempts"]` and
carries `# noqa: SLF001` with the same justification wording as `read_sdk_attempt`
(`worker.py:406`) — same private attribute, same reason, already ledgered in
[`docs/UPSTREAM.md`](../UPSTREAM.md). This is the ONLY new ignore this plan authorizes.
Do not add any other.

- [ ] **Step 1: Widen `absurd_params(max_attempts=...)` to accept None**

`params.py:91-108` types it `int` in both overloads and the implementation, but SQL NULL
is a real Absurd feature — it means retry forever, which the terminal predicate below
depends on. Change all three signatures to `int | None = ...`. The runtime already
passes None through (only the `NOT_SET` sentinel is filtered, `params.py:137-138`), so
no logic changes.

Add to `tests/core/test_absurd_params.py`, following its existing style:

```python
def test_max_attempts_accepts_none_for_unbounded_retries() -> None:
    task_with_unbounded_attempts = params_module.absurd_params(max_attempts=None).bind(
        tasks.add
    )

    assert task_with_unbounded_attempts.absurd_params == {"max_attempts": None}
```

Check the file's actual import alias for `params` and its assertion idiom before writing
this.

- [ ] **Step 2: Write the failing tests**

```python
import logging

import pytest
from absurd_sdk import RetryStrategy
from django.tasks import TaskResultStatus, task_backends
from django.tasks.signals import task_finished, task_started

from django_absurd import params as params_module
from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_non_final_failed_attempt_sends_nothing(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    finished = utils.RecordingReceiver()
    started = utils.RecordingReceiver()
    two_attempts = params_module.absurd_params(max_attempts=2).bind(tasks.boom)
    with utils.connect_receiver(task_started, started, sender=AbsurdBackend):
        with utils.connect_receiver(task_finished, finished, sender=AbsurdBackend):
            with dj_absurd.freeze_time():
                two_attempts.enqueue()
                # Default retry strategy is kind 'none' -> delay 0, so both attempts are
                # claimable inside one drain; no clock movement needed.
                dj_absurd.drain()

    assert len(started.results) == 2
    assert len(finished.results) == 1
    assert finished.results[0].status == TaskResultStatus.FAILED
    assert len(finished.results[0].errors) == 1


def test_unbounded_attempts_never_report_terminal(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    finished = utils.RecordingReceiver()
    # A backoff is mandatory here: with max_attempts=None and the default delay-0
    # strategy, the burst drain re-claims the failing task forever and the test hangs
    # until pytest-timeout kills it.
    unbounded = params_module.absurd_params(
        max_attempts=None,
        retry_strategy=RetryStrategy(kind="fixed", base_seconds=3600),
    ).bind(tasks.boom)
    with utils.connect_receiver(task_finished, finished, sender=AbsurdBackend):
        with dj_absurd.freeze_time():
            unbounded.enqueue()
            dj_absurd.drain()

    assert finished.results == []


def test_django_logs_error_only_on_the_terminal_attempt(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    two_attempts = params_module.absurd_params(max_attempts=2).bind(tasks.boom)
    with dj_absurd.freeze_time():
        two_attempts.enqueue()
        dj_absurd.drain()

    django_records = [r for r in caplog.records if r.name == "django.tasks"]
    errors = [r for r in django_records if r.levelno >= logging.WARNING]
    assert len(errors) == 1
    assert errors[0].levelno == logging.ERROR
    assert "state=FAILED" in errors[0].getMessage()
    assert errors[0].exc_info is not None
    # The line names the task's OWN exception: the send happens inside the handler's
    # except arm, so sys.exc_info() is what the task raised.
    assert errors[0].exc_info[0] is ValueError


def test_the_persisted_traceback_holds_only_the_tasks_own_failure(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """The SDK formats the live exception's __traceback__ into failure_reason, so
    anything django-absurd runs while REPORTING that failure must stay off it.
    """
    one_attempt = params_module.absurd_params(max_attempts=1).bind(tasks.boom)
    with dj_absurd.freeze_time():
        result = one_attempt.enqueue()
        dj_absurd.drain()

    failure = dj_absurd.get_result(result.id).failure
    assert failure is not None
    assert "send_finished_if_terminal" not in failure["traceback"]
    assert "send_task_signal" not in failure["traceback"]
    # Ends at the task's own exception, so nothing was appended after the handler saw
    # it.
    assert failure["traceback"].strip().endswith("ValueError: boom")
    assert task_backends["default"].get_result(result.id).errors[0].traceback
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/core/test_signals_finished_failure.py -v` Expected: the first
three FAIL (no `task_finished` at all, no ERROR record);
`test_unbounded_attempts_never_report_terminal` PASSES as a guard, and so does the
traceback test — it is a guard on what the reporting path must never touch.

- [ ] **Step 4: Implement the predicate and the failure send**

In `build_handler`'s `except Exception` arm, before the existing `raise`:

- Add `read_sdk_max_attempts(ctx)` beside `read_sdk_attempt` (helpers below their
  caller), with the pre-authorized `noqa: SLF001`.
- Terminal is `max_attempts is not None and attempt >= max_attempts`. Comment the NULL
  case with the SQL it mirrors: `fail_run` retries when
  `v_max_attempts is null or v_next_attempt <= v_max_attempts`, so NULL is
  retry-forever.
- Terminal: `object.__setattr__` for `status` (`FAILED`) and `finished_at`, append one
  `TaskError` built from the live exception exactly as `ImmediateBackend` does —
  `exception_class_path` from `type(exc).__module__` + `__qualname__`, `traceback` from
  `format_exception(exc)`. Then call `dispatch.send_task_signal(task_finished, ...)`.
- Non-terminal: send nothing. A failed non-final attempt is READY, not FAILED.
- Leave the existing `raise` untouched — the SDK must receive the original exception,
  and nothing on the reporting path may touch its `__traceback__`: the SDK's
  `_serialize_error` formats that into the stored `failure_reason`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/core/test_signals_finished_failure.py -v` Expected: PASS, all
five.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/params.py django_absurd/worker.py tests/core/test_signals_finished_failure.py tests/core/test_absurd_params.py
git commit -m "feat: send task_finished on a terminal failure, with its traceback"
```

---

### Task 6: Absurd's own control-flow states

**Files:**

- Modify: `django_absurd/worker.py` (the
  `except (SuspendTask, CancelledTask, FailedTask)` arm)
- Modify: `tests/core/test_signals_finished_success.py`

**Interfaces:**

- Consumes: `dispatch.send_task_signal` (Task 1).
- Produces: nothing.

- [ ] **Step 0: Three carried-over findings from earlier reviews**

These were deferred only because `worker.py` was held by another task. Each is small and
each has a test consequence, so do them first and let the same gates cover them.

1. **`last_attempted_at` contradicts the design.** The spec's payload table promises
   `started_at`/`last_attempted_at` = now on `task_started`, but
   `build_running_task_result` sets `last_attempted_at=None`. Django's own backend sets
   it to the attempt's start (`immediate.py`, alongside `started_at`). Fix the CODE, not
   the spec — set it to the same instant as `started_at` — and extend an existing
   `task_started` assertion in `tests/core/test_signals_started.py` to pin it.
2. **`_return_value` is not normalized.** Django applies `normalize_json` to the raw
   return value before storing it; we hand the raw object to the receiver. So a task
   returning a tuple gives the finish receiver a tuple while `get_result().return_value`
   yields a list. Apply the same normalization Django does, and assert it with a task
   whose return value changes shape under normalization (a tuple is the obvious case;
   add a small fixture task locally if `tests/tasks.py` has none).
3. **Rot-prone citation.** `tests/utils.py` cites `immediate.py:71-73` by line number,
   which will drift with any Django release. Cite the file and the behaviour instead.

- [ ] **Step 1: Send nothing from the control-flow arm**

`SuspendTask`, `CancelledTask` and `FailedTask` all send nothing, so
`except (SuspendTask, CancelledTask, FailedTask) as exc` keeps its `logger.info` on the
`django_absurd` logger and its bare `raise`, and gains no send.

The arm is still load-bearing and must not be deleted: all three are `Exception`
subclasses, so without it they fall into `except Exception`, which builds a `FAILED`
payload and sends `task_finished` — reporting an ending Absurd decided as the task
failing.

**The rule, stated once:** Django's task signals report the lifecycle Django knows —
enqueued, started, and finished on success or on a failure the task's own code raised.
Endings Absurd decides on its own are outside what Django's task signals describe.

Why AB001/AB002 are not reported despite being the two Absurd endings a handler CAN
observe: `task_finished` already sends nothing for the `max_delay` pre-claim sweep,
claim expiry, an admin cancel of an unclaimed task, `max_duration` inside `fail_run`, a
queue mismatch and an unknown-task defer failure. Reporting the mid-run cancel too would
mean a cancelled task gets a Django `FAILED` line only when the cancel happened to land
while a worker was mid-run — logging that depends on a race, and one rule replaced by
two plus six exceptions.

- [ ] **Step 2: Pin the suspended-run behaviour**

One test, beside the other sleeping-task assertions: a task that suspends on a durable
sleep sends no `task_finished`. That is what pins `SuspendTask` not reaching the
`except Exception` arm.

`CancelledTask`/`FailedTask` get no test. They send nothing, and the arm that swallows
them is the same one the sleep test covers; reaching them specifically would need a task
that sabotages its own run (cancelling itself through a fresh SDK client, or
`absurd.fail_run` on `ctx._task["run_id"]` by raw SQL) in order to assert an absence.

- [ ] **Step 3: Whole suite plus coverage, then commit**

```bash
uv run pytest tests/core --cov=django_absurd --cov-branch -q
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/worker.py tests/core/test_signals_finished_success.py
git commit -m "feat: leave Absurd-decided endings out of Django's task signals"
```

`worker.py` should be fully covered — the control-flow arm has no branches of its own
and the sleep test enters it.

---

### Task 7: The read path's twin of the rebind bug

Found by Task 3's review, not by the original plan. Task 3 fixed a queue-only
`.using(queue_name=...)` rebind in the worker: `Task.using` is a bare
`dataclasses.replace` and `__post_init__` re-validates against the **definition's**
backend alias, so rebinding queue-only raises `InvalidTaskBackend` when that alias is
not configured. `backends.build_task_result` has the identical rebind and was left alone
— deliberately, since it is a different seam with no covering test.

It is not theoretical. A beat-routed task under a second alias now executes fine, but
`get_result()` on it imports the task, sees `task_obj.queue_name != queue`, rebinds
queue-only, and raises — an uncurated crash on a pure read. That also hollows out Task
2's promise that the worker-side id round-trips through `get_result()`.

**Files:**

- Modify: `django_absurd/backends.py` (`build_task_result`, the rebind near
  `backends.py:338`)
- Modify: `tests/core/test_signals_worker_result.py` (or the multi-alias test module
  that already configures a second backend — find it first and follow its setup)

- [ ] **Step 1: Write the failing test**

Drive it through the real path, not by calling `build_task_result` directly: enqueue a
task whose definition alias differs from the alias the run is routed through, drain it,
then call `get_result()` via the routed backend. Look at how
`tests/core/test_scheduler.py`'s `test_beat_routes_task_to_queue_non_default_alias` sets
up its aliases and reuse that shape — that is the test Task 3's regression surfaced
through.

Assert `get_result()` returns a `TaskResult` whose `.status` is `SUCCESSFUL` — today it
raises `InvalidTaskBackend` instead.

- [ ] **Step 2: Run to verify failure**

Expected: `InvalidTaskBackend`, naming the unconfigured definition alias. If you instead
see a passing test, you have not reproduced the condition — the definition alias must be
one the settings do NOT configure.

- [ ] **Step 3: Fix it the same way Task 3 did**

Move the alias with the queue, mirroring `worker.py:328-329`: compare
`(task_obj.queue_name, task_obj.backend)` against `(queue, backend.alias)` as a tuple
and pass both to `.using()`. Same reasoning as the worker: a result read through a given
backend belongs to that backend, so forcing the alias cannot mislabel a legitimately
foreign task.

- [ ] **Step 4: Run to verify pass**

Run the new test plus `tests/core/test_results.py`, `tests/core/test_run_after.py` and
`tests/core/test_scheduler.py` — every existing consumer of `build_task_result`.

- [ ] **Step 5: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/backends.py tests/core/
git commit -m "fix: keep the backend alias with the queue when rebinding a result's task"
```

---

### Task 8: Documentation

**Files:**

- Modify: `django_absurd/AGENTS.md`
- Modify: `docs/web/` (the page covering enqueueing and results — locate it first)
- Modify: `README.md` if it lists backend capabilities

- [ ] **Step 1: Invoke the docs skill**

Run the `sync-docs` skill. This change touches user-facing behaviour, so it owns the
docs sweep rather than hand-editing one file.

- [ ] **Step 2: Cover exactly these points**

Each is a promise a user can otherwise get wrong:

- The three signals are sent so Django's own `django.tasks` logging works. They are not
  offered as an extension point, and the docs carry no receiver-authoring material and
  no example receiver.
- A worker-side receiver runs on the worker's event loop, where Django's ORM refuses to
  run at all, and inside the run's claim lease — a slow receiver ages the claim toward
  `claim_timeout`.
- A receiver's exception is logged and contained, never fatal to the task.
- `task_started` fires per execution episode, so a task with a durable sleep sends
  several for one attempt. **Do not build counters or gauges on it.**
- The payload may be mutated after a receiver returns, as Django's own backends do it.
  Receivers must snapshot what they need and rely on neither send-time state nor object
  identity across signals: `task_enqueued` carries a different object, and each handler
  entry of a sleeping task builds a fresh one, so identity holds only started→finished
  within one entry.
- `task_finished` reports a success or a failure the task's own code raised, once it is
  terminal. An ending Absurd decided itself is outside what these signals describe, with
  the list of those endings.
- A deferred (`run_after`) enqueue sends two `task_enqueued`; the discriminator is
  `task_result.task.run_after`, and the wrapper's id never receives a start or finish.
- pg_cron-scheduled tasks send no `task_enqueued`; beat schedules do.
- A rollback after enqueue leaves a `task_enqueued` already sent.
- `absurd_params(max_attempts=None)` is now typed as well as supported: it means retry
  forever, and therefore no `task_finished`.
- Release note: `TaskContext.task_result.id` changes shape from a bare uuid to
  `"queue:uuid"` — the form that round-trips through `get_result()`.

- [ ] **Step 3: Commit**

```bash
uv run pre-commit run --all-files
git add -A
git commit -m "docs: document the task signals AbsurdBackend now sends"
```

---

## Finishing

Do NOT push or open a PR. Local review flow: `revdiff` against `origin/main` after
`git fetch`, then an adversarial review with the best available model. Merging is the
maintainer's call.
