# Durable test clock — `absurd` fixture: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement task-by-task. Steps use checkbox (`- [ ]`)
> syntax.

**Goal:** ship an `absurd` pytest fixture that advances durable time deterministically,
so sleep / event-timeout / retry-backoff / claim-expiry / cancellation behavior is
testable without wall-clock waiting.

**Architecture:** pin BOTH clocks that gate a sleeping run — Postgres via Absurd's own
`absurd.fake_now` GUC set at DATABASE level (the worker opens its own connection per
drain), and Python via time-machine `tick=False`. `fake_now` never ticks, so Python must
be frozen too. Reads happen on fresh UTC-pinned connections. `drain()` returns per-run
records; `get_result()` returns a task-level record.

**Tech stack:** Django 6.0, psycopg 3, absurd-sdk, pytest + pytest-django, time-machine
(runtime-detected, NOT bundled).

**Spec:** `docs/specs/2026-07-28-durable-test-clock-design.md`. Read it before starting;
it carries the reasoning and the measured evidence behind every rule below.

## Global constraints

- Floor Django 6.0 / Python 3.12. psycopg (v3) backend only.
- `import typing as t` — never `from typing import X`. Absolute imports only.
- Functions contain a verb. No leading-underscore module constants/helpers. Helpers go
  BELOW their public callers.
- pytest, function-based only. Autouse `_enable_db` gives DB access — do NOT add
  `@pytest.mark.django_db`; add `(transaction=True)` only when commits/DDL needed.
- No monkeypatching / `unittest.mock.patch`. ONE deliberate exception, spec-sanctioned:
  the `sys.meta_path` finder in Task 3's missing-dependency test.
- Assert the COMPLETE error message, never a fragment.
- Alphabetize `@pytest.mark.parametrize` values and each test's own fixture parameters.
- mypy strict. Narrow `# type: ignore[...]` allowed only where a test deliberately
  passes something the checker rejects.
- No ruff ignore / `noqa` without asking first.
- 100% statement+branch coverage on lines this patch adds.
- Gates before a behavior commit: `uvx --with tox-uv tox -e dev` and
  `uv run pre-commit run --all-files`. Iterating: `uv run pytest <path> -v`.
- Compose services must be up: `docker compose up -d db db_pg_cron`.
- Every new test file: `pytestmark = pytest.mark.django_db(transaction=True)` — the
  fixture requires it and refuses otherwise.

## File structure

| File                                                                   | Responsibility                                                                                                                                                           |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `django_absurd/test.py` (modify)                                       | public test surface: `RunSnapshot`, `TaskSnapshot`, the runtime object, fresh-connection reader. Already hosts `install_absurd_cleanup` / `flush_absurd_after_teardown`. |
| `django_absurd/flush.py` (modify)                                      | `flush_absurd_state` gains the defensive GUC reset — it lives HERE, not in `test.py`                                                                                     |
| `django_absurd/pytest_plugin.py` (modify)                              | `absurd` fixture, `absurd_drain_queue` delegate, session-start GUC sweep                                                                                                 |
| `django_absurd/worker.py` (modify)                                     | widen `drain_queue` / `run_burst_worker` to return claim rows                                                                                                            |
| `tests/tasks.py` (modify)                                              | new fixture tasks the new tests execute                                                                                                                                  |
| `tests/core/test_absurd_fixture.py` (create)                           | read surface, drain return, transaction guard, emit, alias                                                                                                               |
| `tests/core/test_durable_clock.py` (create)                            | freeze/advance against real durable primitives                                                                                                                           |
| `tests/core/test_pytest_plugin.py` (modify)                            | teardown reset + session-start sweep                                                                                                                                     |
| `docs/web/testing.md`, `django_absurd/AGENTS.md`, `CLAUDE.md` (modify) | user + maintainer docs                                                                                                                                                   |

Phase 2 (own plan, NOT this one): freezegun→time-machine migration, converting
`tests/core/test_durable.py`'s four `time.sleep(2)` recipes, raising the fixture tasks'
1.5s sleeps, and replacing 24 `utils.get_task_result` call sites. Spec section "Adopt in
the existing suite".

---

## Task 1: read surface — snapshots on a fresh UTC-pinned connection

Delivers `absurd.get_result()` plus the transaction guard. No clock yet.

**Files:**

- Modify: `django_absurd/test.py`
- Modify: `django_absurd/pytest_plugin.py`
- Modify: `tests/tasks.py`
- Test: `tests/core/test_absurd_fixture.py` (create)

**Interfaces:**

- Consumes: `django_absurd.connection.register_jsonb_loader`, `django.db.connections`,
  existing `tests.utils.run_absurd_worker`.
- Produces:
  - `django_absurd.test.TaskSnapshot` — frozen dataclass, fields `queue: str`,
    `task_id: uuid.UUID`, `task_name: str`, `args: list[t.Any]`,
    `kwargs: dict[str, t.Any]`, `state: str`, `attempts: int`,
    `enqueued_at: dt.datetime`, `result: t.Any | None`, `failure: t.Any | None`.
  - `django_absurd.test.AbsurdTestRuntime` with
    `get_result(task_id: str | uuid.UUID, queue: str = "default") -> TaskSnapshot | None`.
  - `absurd` pytest fixture yielding `AbsurdTestRuntime`.
  - `django_absurd.test.open_test_connection(alias: str)` — context manager yielding a
    cursor on a fresh UTC-pinned connection.

- [ ] **Step 1: add the fixture tasks these tests execute**

No new fixture tasks are needed. `tests/tasks.py` already has `add` (completes) and
`boom` (raises `ValueError("boom")` under the default retry policy) — use those. Do NOT
add a near-duplicate of either.

- [ ] **Step 2: write the failing tests**

Create `tests/core/test_absurd_fixture.py`:

```python
import typing as t
import uuid

import psycopg
import pytest
from django.core.management import call_command
from django.db import connections, transaction

from django_absurd.test import AbsurdTestRuntime, TaskSnapshot
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_get_result_reports_a_completed_task(absurd: AbsurdTestRuntime) -> None:
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()

    snapshot = absurd.get_result(result.id)

    assert snapshot is not None
    assert snapshot.queue == "default"
    assert snapshot.task_name == "tests.tasks.add"
    assert snapshot.args == [2, 3]
    assert snapshot.kwargs == {}
    assert snapshot.state == "completed"
    assert snapshot.result == 5
    assert snapshot.failure is None
    assert snapshot.attempts == 1
    assert isinstance(snapshot.task_id, uuid.UUID)


def test_get_result_reports_a_failed_task_with_its_failure(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.boom.enqueue()
    utils.run_absurd_worker()

    snapshot = absurd.get_result(result.id)

    assert snapshot is not None
    assert snapshot.state == "failed"
    assert snapshot.result is None
    assert snapshot.failure is not None
    assert snapshot.failure["name"] == "ValueError"
    assert snapshot.failure["message"] == "boom"


def test_get_result_decodes_jsonb_to_python_objects(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.create_payload.enqueue({"k": "v", "n": 7})
    utils.run_absurd_worker()

    snapshot = absurd.get_result(result.id)

    assert snapshot is not None
    assert snapshot.args == [{"k": "v", "n": 7}]
    assert not isinstance(snapshot.args[0], str)
    assert isinstance(snapshot.result, int)


def test_get_result_returns_none_for_an_unknown_task(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")

    assert absurd.get_result(uuid.uuid4()) is None


def test_get_result_tolerates_params_that_are_not_our_shape(
    absurd: AbsurdTestRuntime,
) -> None:
    """A queue shared with raw-SDK producers can hold any JSON in params."""
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()
    task_id = str(result.id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    conn = psycopg.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "update absurd.t_default set params = %s where task_id = %s",
                ["[1, 2, 3]", task_id],
            )
    finally:
        conn.close()

    snapshot = absurd.get_result(result.id)

    assert snapshot is not None
    assert snapshot.args == []
    assert snapshot.kwargs == {}


def test_get_result_accepts_a_bare_uuid(absurd: AbsurdTestRuntime) -> None:
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()
    bare = str(result.id).rsplit(":", 1)[-1]

    snapshot = absurd.get_result(bare)

    assert snapshot is not None
    assert snapshot.state == "completed"


def test_get_result_inside_an_open_transaction_raises(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()

    with (
        transaction.atomic(),
        pytest.raises(
            RuntimeError,
            match=(
                r"django-absurd: get_result\(\) ran inside an open transaction, where "
                r"uncommitted rows are invisible to Absurd's own connection\. Use "
                r"@pytest\.mark\.django_db\(transaction=True\) and call outside "
                r"transaction\.atomic\(\)\."
            ),
        ),
    ):
        absurd.get_result(result.id)
```

Add a non-transactional guard test in the same file — it needs its own marker, so give
it an explicit override:

```python
@pytest.mark.django_db(transaction=False)
def test_get_result_in_a_plain_db_test_raises(absurd: AbsurdTestRuntime) -> None:
    with pytest.raises(
        RuntimeError,
        match=(
            r"django-absurd: get_result\(\) ran inside an open transaction, where "
            r"uncommitted rows are invisible to Absurd's own connection\. Use "
            r"@pytest\.mark\.django_db\(transaction=True\) and call outside "
            r"transaction\.atomic\(\)\."
        ),
    ):
        absurd.get_result(uuid.uuid4())
```

- [ ] **Step 3: run the tests, confirm they fail**

Run: `uv run pytest tests/core/test_absurd_fixture.py -v` Expected: collection error —
`cannot import name 'AbsurdTestRuntime' from 'django_absurd.test'`.

- [ ] **Step 4: implement the fresh-connection reader**

In `django_absurd/test.py`, add a context manager that opens a short-lived connection
for reads. Build params from `connections[alias].get_connection_params()`, then remove
BOTH `cursor_factory` and `context`. Dropping `context` is load-bearing, not tidiness:
Django's adapter relabels timestamptz with `replace(tzinfo=...)`, which is only correct
when the session is already UTC — inherited unpinned on a non-UTC server it yields
wall-clock digits mislabeled UTC, a different instant. Connect autocommit,
`SET TIME ZONE 'UTC'`, apply `register_jsonb_loader`, yield a cursor, close in
`finally`. Mirror the shape of `django_absurd/connection.py:open_central_connection`.

- [ ] **Step 5: implement `TaskSnapshot` and `get_result`**

Add the frozen dataclass with the fields from Interfaces. Add `AbsurdTestRuntime`
holding the backend alias and queue defaults.

`get_result` accepts either a Django `TaskResult.id` (`queue:uuid`) or a bare uuid —
split on the last `:` as `tests/utils.py` already does. It checks the transaction guard
first (Task 1 step 6), then runs ONE query against `t_<q>` left-joining `r_<q>` on
`last_attempt_run` for `failure_reason`, on the fresh connection. Compose table names
with `psycopg.sql.Identifier`. Decode `params` defensively with exactly ONE branch — an
`isinstance(params, Mapping)` guard around `params.get("args", [])` /
`params.get("kwargs", {})`. Two nested conditions (mapping AND carries-the-keys) leave
the mapping-without-keys arm unreachable by any test, and the patch-coverage gate fails
on it. Return `None` when no row.

- [ ] **Step 6: implement the transaction guard**

A module-level function in `django_absurd/test.py`, called at the top of every public
read/drain method — call time, never fixture setup. At setup the check would be
unreliable in a user project with no autouse db fixture; a method body always runs after
every db fixture. Condition: `connections[alias].in_atomic_block`. Raise `RuntimeError`
with the message the tests assert verbatim, naming the operation. Word it as "ran inside
an open transaction", never "the marker is missing" — a legitimate `transaction=True`
test calling inside `atomic()` hits the same invisibility.

- [ ] **Step 7: expose the `absurd` fixture**

In `django_absurd/pytest_plugin.py`, add a function-scoped `absurd` fixture that
constructs `AbsurdTestRuntime`. Keep the module's import-safety rule: imports of
anything Django-touching stay INSIDE the function body (this module is imported by
pytest's plugin bootstrap in every project that has django-absurd installed).

- [ ] **Step 8: run the tests, confirm they pass**

Run: `uv run pytest tests/core/test_absurd_fixture.py -v` Expected: PASS, 8 tests.

- [ ] **Step 9: gates + commit**

```bash
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git add django_absurd/test.py django_absurd/pytest_plugin.py tests/tasks.py tests/core/test_absurd_fixture.py
git commit -m "feat(test): absurd fixture with get_result snapshots"
```

---

## Task 2: `drain()` returning per-run records

**Files:**

- Modify: `django_absurd/worker.py`
- Modify: `django_absurd/test.py`
- Modify: `django_absurd/pytest_plugin.py`
- Modify: `django_absurd/management/commands/absurd_worker.py:93` — consumes the return
- Modify: `tests/core/test_worker.py:442-446` — asserts the return
- Modify: `tests/tasks.py`
- Test: `tests/core/test_absurd_fixture.py`

**Interfaces:**

- Consumes: Task 1's `open_test_connection`, transaction guard, `AbsurdTestRuntime`.
- Produces:
  - `django_absurd.test.RunSnapshot` — frozen dataclass, fields `queue: str`,
    `run_id: uuid.UUID`, `task_id: uuid.UUID`, `task_name: str`, `args: list[t.Any]`,
    `kwargs: dict[str, t.Any]`, `attempt: int`, `state: str`, `result: t.Any | None`,
    `failure: t.Any | None`.
  - `AbsurdTestRuntime.drain(queue: str = "default") -> list[RunSnapshot]`.
  - `django_absurd.worker.run_burst_worker(queue, *, options) -> tuple[SyncResult, list[DrainedRun]]`
    — now a tuple, so both existing consumers need a one-line update (Step 4).
  - `django_absurd.worker.DrainedRun` — frozen dataclass, fields `run_id: uuid.UUID`,
    `task_id: uuid.UUID`, `task_name: str`, `params: t.Any`, `attempt: int`,
    `state: str`, `result: t.Any | None`, `failure: t.Any | None`. Lives in `worker.py`,
    NOT in `test.py`: `test.py` imports `django.test`, which production code must not
    pull in. `drain_queue` returns `list[DrainedRun]`; `run_burst_worker` returns them
    alongside its existing `SyncResult`.

- [ ] **Step 1: add the fixture tasks these tests execute**

`tests/tasks.py`:

```python
@task
def spawn_child_then_return(value: int) -> str:
    run_child.enqueue(value)
    return "spawned"


@task
def run_child(value: int) -> int:
    return value * 2


@task
@absurd_params(retry_strategy=RetryStrategy(kind="fixed", base_seconds=0))
def fail_twice_then_succeed() -> str:
    RETRY_CALLS["n"] += 1
    if RETRY_CALLS["n"] < 3:
        msg = f"attempt {RETRY_CALLS['n']} fails"
        raise ValueError(msg)
    return "third-time-lucky"
```

Add `RETRY_CALLS: dict[str, int] = {"n": 0}` beside the existing `SYNC_STEP_CALLS`
counter and reset it in each test that uses it (the existing `SYNC_STEP_CALLS` tests
show the pattern).

- [ ] **Step 2: write the failing tests**

Append to `tests/core/test_absurd_fixture.py`:

```python
def test_drain_returns_nothing_when_the_queue_is_empty(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")

    assert absurd.drain() == []


def test_drain_returns_one_record_per_run_in_claim_order(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    tasks.add.enqueue(2, 3)
    tasks.add.enqueue(4, 5)

    drained = absurd.drain()

    assert [(run.task_name, run.args, run.attempt, run.state) for run in drained] == [
        ("tests.tasks.add", [2, 3], 1, "completed"),
        ("tests.tasks.add", [4, 5], 1, "completed"),
    ]
    assert [run.result for run in drained] == [5, 9]


def test_drain_reports_a_suspended_run_as_sleeping(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    tasks.ssleep_for_once.enqueue("k")

    drained = absurd.drain()

    assert [(run.task_name, run.state) for run in drained] == [
        ("tests.tasks.ssleep_for_once", "sleeping")
    ]


def test_drain_returns_a_spawned_child_in_the_same_drain(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    tasks.spawn_child_then_return.enqueue(21)

    drained = absurd.drain()

    assert [run.task_name for run in drained] == [
        "tests.tasks.spawn_child_then_return",
        "tests.tasks.run_child",
    ]
    assert drained[1].result == 42
    assert absurd.drain() == []


def test_drain_returns_every_attempt_of_a_default_retry_burn(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.boom.enqueue()

    drained = absurd.drain()

    assert [run.attempt for run in drained] == [1, 2, 3, 4, 5]
    assert {run.state for run in drained} == {"failed"}
    assert drained[0].failure is not None
    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.state == "failed"
    assert snapshot.attempts == 5


def test_drain_reports_each_attempt_of_a_retry_sequence(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    tasks.RETRY_CALLS["n"] = 0
    tasks.fail_twice_then_succeed.enqueue()

    drained = absurd.drain()

    assert [(run.attempt, run.state) for run in drained] == [
        (1, "failed"),
        (2, "failed"),
        (3, "completed"),
    ]
    assert [
        None if run.failure is None else run.failure["message"] for run in drained
    ] == ["attempt 1 fails", "attempt 2 fails", None]
    assert drained[2].result == "third-time-lucky"


def test_drain_returns_the_same_run_twice_when_an_emit_wakes_it(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    tasks.sawait_event_once.enqueue("order.packed:same-drain")
    tasks.semit_event_once.enqueue("order.packed:same-drain", {"tracking": "abc"})

    drained = absurd.drain()

    waiter_runs = [run for run in drained if run.task_name.endswith("sawait_event_once")]
    assert [run.state for run in waiter_runs] == ["sleeping", "completed"]
    assert waiter_runs[0].run_id == waiter_runs[1].run_id


def test_drain_inside_an_open_transaction_raises(absurd: AbsurdTestRuntime) -> None:
    call_command("absurd_sync_queues")

    with (
        transaction.atomic(),
        pytest.raises(
            RuntimeError,
            match=(
                r"django-absurd: drain\(\) ran inside an open transaction, where "
                r"uncommitted rows are invisible to Absurd's own connection\. Use "
                r"@pytest\.mark\.django_db\(transaction=True\) and call outside "
                r"transaction\.atomic\(\)\."
            ),
        ),
    ):
        absurd.drain()
```

- [ ] **Step 3: run the tests, confirm they fail**

Run: `uv run pytest tests/core/test_absurd_fixture.py -v -k drain` Expected: FAIL —
`AttributeError: 'AbsurdTestRuntime' object has no attribute 'drain'`.

- [ ] **Step 4: widen the worker's burst path**

`drain_queue` currently counts runs and returns an `int`; `run_burst_worker` discards it
and returns the provisioning `SyncResult`. Change `drain_queue` to accumulate, for each
claimed row it executes, the identity the SDK's `ClaimedTask` already carries
(`run_id`/`task_id`/`task_name`/`attempt`/`params`) — no extra query for identity. Read
each run's own `state`/`result`/`failure_reason` immediately AFTER that run executes,
never in one batch at drain end: an `await_event` waiter re-arms its own run, so a
final-state batch read would rewrite history for an earlier appearance of the same
`run_id` (the test in Step 2 covers this).

Have `run_burst_worker` return `tuple[SyncResult, list[DrainedRun]]`. Two callers
consume the old single return and MUST be updated in this task, or the management
command breaks and a sibling test fails with no obvious cause:

- `django_absurd/management/commands/absurd_worker.py:93` —
  `result = run_burst_worker(...)` becomes `result, _ = run_burst_worker(...)`, feeding
  `report_sync_result` unchanged.
- `tests/core/test_worker.py:446` in
  `test_run_burst_worker_processes_a_task_and_returns_sync_result` — unpack the tuple,
  keeping its existing `SyncResult` assertions.

- [ ] **Step 5: implement `RunSnapshot` and `drain`**

Add the frozen dataclass. `drain` checks the guard, calls the widened burst path for the
queue, and maps each collected row to a `RunSnapshot`, decoding `params` with the same
defensive helper Task 1 added. Preserve claim order.

- [ ] **Step 6: run the tests, confirm they pass**

Run: `uv run pytest tests/core/test_absurd_fixture.py -v` Expected: PASS, 16 tests.

- [ ] **Step 7: gates + commit**

```bash
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git add django_absurd/worker.py django_absurd/test.py django_absurd/pytest_plugin.py tests/tasks.py tests/core/test_absurd_fixture.py
git commit -m "feat(test): drain returns per-run snapshots"
```

---

## Task 3: the clock — `freeze_at`, `advance`, `now`

**Files:**

- Modify: `django_absurd/test.py`
- Modify: `pyproject.toml` (dev dependency)
- Modify: `tests/tasks.py` — sync fixture tasks only; the async ones belong to phase 2
- Test: `tests/core/test_durable_clock.py` (create)

**Interfaces:**

- Consumes: Task 1's `open_test_connection` + guard, Task 2's `drain`.
- Produces: `AbsurdTestRuntime.freeze_at(when: dt.datetime) -> None`,
  `AbsurdTestRuntime.advance(delta: dt.timedelta) -> None`, `AbsurdTestRuntime.now`
  property returning `dt.datetime` (UTC-aware).

- [ ] **Step 1: add the dev dependencies**

Add BOTH `time-machine` and `pytest-timeout` to the dev dependency group in
`pyproject.toml` (leave `freezegun` alone — its removal is phase 2), then `uv lock`.
time-machine stays a DEV dep only: never bundled, never an extra. pytest-timeout is not
housekeeping — every run command below passes `--timeout`, because a half-applied clock
deadlocks a sync drain permanently, and without the plugin those runs die on
`pytest: error: unrecognized arguments: --timeout=60`.

- [ ] **Step 2: add the long-sleep fixture tasks**

`tests/tasks.py` — durations long enough that no wall-clock wait could ever satisfy
them, which is the point:

`tests/tasks.py` also needs a task whose cancellation policy can fire once time moves,
and `tests/utils.py` a plain function that takes a lease without running the task
(claiming is two lines — inline the cursor there rather than hiding it further):

```python
@task
@absurd_params(cancellation=CancellationPolicy(max_delay=60))
def cancellable_after_a_minute() -> t.Never:
    msg = "cancelled before it ever ran"
    raise NotImplementedError(msg)
```

`utils.claim_one_run(queue, *, claim_timeout)` calls `absurd.claim_task` through a
short-lived connection so a run holds a lease that the sweep can later expire.
`tests/tasks.py` already has `cancellable` with `max_duration=30`, but it carries a
`NotImplementedError` body and no `max_delay` — leave it alone.

```python
WEEK_SECONDS = 7 * 24 * 3600


@task
def sleep_a_week() -> str:
    get_absurd_context().sleep_for("nap", WEEK_SECONDS)
    return "woke"


@task
def sleep_twice() -> str:
    context = get_absurd_context()
    context.sleep_for("first", WEEK_SECONDS)
    context.sleep_for("second", 3 * 24 * 3600)
    return "woke-twice"


@task
@absurd_params(retry_strategy=RetryStrategy(kind="fixed", base_seconds=3600))
def fail_with_long_backoff() -> t.Never:
    msg = "boom"
    raise ValueError(msg)
```

- [ ] **Step 3: write the failing tests**

Create `tests/core/test_durable_clock.py`:

```python
import datetime as dt
import importlib.abc
import importlib.machinery
import sys
import typing as t
import zoneinfo

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connections

from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


def test_a_week_long_sleep_resumes_after_advancing_a_week(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.sleep_a_week.enqueue()
    assert [run.state for run in absurd.drain()] == ["sleeping"]

    absurd.advance(dt.timedelta(days=7))

    assert [run.state for run in absurd.drain()] == ["completed"]
    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.result == "woke"


def test_advancing_short_of_the_wake_leaves_the_task_sleeping(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.sleep_a_week.enqueue()
    absurd.drain()
    real_before = dt.datetime.now(dt.UTC)

    absurd.advance(dt.timedelta(days=6))

    # Without this, a no-op advance() would pass the rest of the test.
    assert absurd.now - real_before >= dt.timedelta(days=6)
    assert absurd.drain() == []
    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.state == "sleeping"


def test_a_chain_of_two_sleeps_needs_two_advances(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.sleep_twice.enqueue()
    absurd.drain()

    absurd.advance(dt.timedelta(days=7))
    assert [run.state for run in absurd.drain()] == ["sleeping"]

    absurd.advance(dt.timedelta(days=3))
    assert [run.state for run in absurd.drain()] == ["completed"]
    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.result == "woke-twice"


def test_an_await_event_timeout_fires_after_advancing_past_it(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.sawait_event_timeout.enqueue(
        "order.packed:never-arrives", timeout=tasks.WEEK_SECONDS
    )
    assert [run.state for run in absurd.drain()] == ["sleeping"]

    absurd.advance(dt.timedelta(days=8))
    absurd.drain()

    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.result == "timed-out"


def test_a_retry_backoff_runs_the_next_attempt_after_advancing(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.fail_with_long_backoff.enqueue()
    assert [run.attempt for run in absurd.drain()] == [1]
    mid_backoff = absurd.get_result(result.id)
    assert mid_backoff is not None
    assert mid_backoff.state == "sleeping"
    assert mid_backoff.failure is None

    absurd.advance(dt.timedelta(hours=1, seconds=1))

    assert [run.attempt for run in absurd.drain()] == [2]


def test_a_cancelled_task_produces_an_empty_drain(
    absurd: AbsurdTestRuntime,
) -> None:
    """Cancellation happens inside claim_task, before anything is claimed."""
    call_command("absurd_sync_queues")
    result = tasks.cancellable_after_a_minute.enqueue()

    absurd.advance(dt.timedelta(minutes=2))

    assert absurd.drain() == []
    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.state == "cancelled"


def test_an_expired_claim_is_swept_after_advancing_past_the_lease(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(2, 3)
    utils.claim_one_run("default", claim_timeout=3600)

    absurd.advance(dt.timedelta(hours=2))
    absurd.drain()

    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.state == "completed"
    assert snapshot.attempts == 2


def test_freeze_at_stamps_the_frozen_instant_on_enqueue(
    absurd: AbsurdTestRuntime,
) -> None:
    call_command("absurd_sync_queues")
    absurd.freeze_at(FROZEN)

    result = tasks.add.enqueue(2, 3)

    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.enqueued_at == FROZEN


def test_now_is_real_until_frozen_then_exactly_the_frozen_instant(
    absurd: AbsurdTestRuntime,
) -> None:
    before = absurd.now
    assert before.utcoffset() == dt.timedelta(0)
    assert abs(before - dt.datetime.now(dt.UTC)) < dt.timedelta(seconds=30)

    absurd.freeze_at(FROZEN)
    assert absurd.now == FROZEN

    absurd.advance(dt.timedelta(days=2))
    assert absurd.now == FROZEN + dt.timedelta(days=2)


def test_advance_without_a_prior_freeze_moves_from_real_now(
    absurd: AbsurdTestRuntime,
) -> None:
    real = dt.datetime.now(dt.UTC)

    absurd.advance(dt.timedelta(days=1))

    assert absurd.now - real >= dt.timedelta(days=1)


def test_freeze_at_honors_a_non_utc_aware_zone(absurd: AbsurdTestRuntime) -> None:
    call_command("absurd_sync_queues")
    chicago = dt.datetime(
        2026, 3, 8, 1, 30, tzinfo=zoneinfo.ZoneInfo("America/Chicago")
    )
    absurd.freeze_at(chicago)

    result = tasks.sleep_a_week.enqueue()
    absurd.drain()
    absurd.advance(dt.timedelta(days=7))
    absurd.drain()

    # advance() moves ABSOLUTE elapsed time; `chicago + timedelta` would be
    # wall-clock arithmetic, an hour short across the spring-forward gap.
    assert absurd.now == chicago.astimezone(dt.UTC) + dt.timedelta(days=7)
    assert absurd.now.utcoffset() == dt.timedelta(0)
    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.enqueued_at == chicago
    assert snapshot.state == "completed"


def test_the_clock_is_correct_on_a_server_whose_timezone_is_not_utc(
    absurd: AbsurdTestRuntime,
) -> None:
    """Regression: Django's adapter relabels timestamptz instead of converting it."""
    dbname = connections["default"].settings_dict["NAME"]
    params: dict[str, t.Any] = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    params.pop("context", None)
    conn = psycopg.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL("alter database {} set timezone = 'America/Chicago'")
                .format(psycopg.sql.Identifier(dbname))
            )
        call_command("absurd_sync_queues")
        absurd.freeze_at(FROZEN)

        result = tasks.add.enqueue(2, 3)
        absurd.drain()

        assert absurd.now == FROZEN
        assert absurd.now.utcoffset() == dt.timedelta(0)
        snapshot = absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.enqueued_at == FROZEN
    finally:
        with conn.cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL("alter database {} reset timezone").format(
                    psycopg.sql.Identifier(dbname)
                )
            )
        conn.close()


def test_freeze_at_rejects_a_naive_datetime(absurd: AbsurdTestRuntime) -> None:
    with pytest.raises(
        TypeError,
        match=(
            r"django-absurd: freeze_at\(\) needs a timezone-aware datetime; a naive one "
            r"is ambiguous and would desynchronise Postgres from Python\. Pass "
            r"tzinfo=datetime\.UTC \(or any zone\)\."
        ),
    ):
        absurd.freeze_at(dt.datetime(2026, 1, 1, 12, 0))  # type: ignore[arg-type]


def test_freeze_at_rejects_a_string(absurd: AbsurdTestRuntime) -> None:
    with pytest.raises(
        TypeError,
        match=(
            r"django-absurd: freeze_at\(\) needs a timezone-aware datetime; a naive one "
            r"is ambiguous and would desynchronise Postgres from Python\. Pass "
            r"tzinfo=datetime\.UTC \(or any zone\)\."
        ),
    ):
        absurd.freeze_at("2026-01-01T12:00:00+00:00")  # type: ignore[arg-type]


def test_advancing_without_time_machine_installed_raises(
    absurd: AbsurdTestRuntime,
) -> None:
    class BlockTimeMachine(importlib.abc.MetaPathFinder):
        def find_spec(
            self,
            fullname: str,
            path: t.Sequence[str] | None = None,
            target: object | None = None,
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname == "time_machine":
                msg = "blocked for this test"
                raise ImportError(msg)
            return None

    blocker = BlockTimeMachine()
    cached = sys.modules.pop("time_machine", None)
    sys.meta_path.insert(0, blocker)
    try:
        with pytest.raises(
            ImproperlyConfigured,
            match=(
                r"django-absurd: advancing durable time needs the time-machine "
                r"package\. Install it in your test environment: pip install "
                r"time-machine\."
            ),
        ):
            absurd.advance(dt.timedelta(days=1))
    finally:
        sys.meta_path.remove(blocker)
        if cached is not None:
            sys.modules["time_machine"] = cached
```

- [ ] **Step 4: run the tests, confirm they fail**

Run: `uv run pytest tests/core/test_durable_clock.py -v --timeout=60` Expected: FAIL —
`AttributeError: 'AbsurdTestRuntime' object has no attribute 'freeze_at'`.

Pass `--timeout` on every run in this task. A half-applied clock (DB ahead of Python)
deadlocks a sync drain permanently and pytest-timeout cannot rescue it — only SIGKILL.
If a run hangs, SIGKILL it, then clear the stranded GUC before rerunning:

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d postgres \
  -c 'alter database "absurd_test_core" reset "absurd.fake_now";'
```

- [ ] **Step 5: implement the apply path**

One internal function owns every clock move; `freeze_at` and `advance` are its two doors
(`advance` = apply(current virtual now + Δ), and before any freeze the current virtual
now is real now).

Order matters and is not arbitrary: move the **Python clock first, then the DB
literal**. A failure between the two must land Python-AHEAD-of-DB, which merely leaves a
run unclaimed; DB-ahead-of-Python is the permanent deadlock.

1. Validate: the argument must be a `datetime` AND aware, else `TypeError` with the
   message the test asserts. Both halves matter — a `str` would otherwise reach
   `astimezone` and die with an unhelpful `AttributeError`, and a string literal that
   survived to the GUC is accepted by `ALTER DATABASE` only to explode inside
   `absurd."current_time"()` on every NEW session, far from the call that caused it.
   Then normalize with `astimezone(dt.UTC)`.
2. Python: lazily `import time_machine` (translating `ImportError` to
   `ImproperlyConfigured` with the install command), then start a
   `travel(..., tick=False)` coordinate on first use and `move_to(...)` on every later
   call. `tick=False` is a correctness requirement, not a preference — `absurd.fake_now`
   is a static literal, so Postgres never ticks, and a ticking Python clock would drift
   out of lockstep.
3. DB, database level:
   `ALTER DATABASE <test_db> SET absurd.fake_now = '<iso with offset>'` on a dedicated
   autocommit connection. `ALTER DATABASE` rejects bind parameters — compose with
   `psycopg.sql.Identifier` and `psycopg.sql.Literal`. Database level, not session: the
   worker opens its own connection per drain, and only a database default reaches it.
   Read the database name at runtime from `connections[alias].settings_dict["NAME"]` so
   xdist's per-worker databases work.
4. DB, session level: also `SET absurd.fake_now` on the Absurd alias' live Django
   connection, so an `enqueue()` after an advance stamps fake time rather than real (a
   database default only reaches NEW sessions).

`now` reads `select absurd.current_time()` through Task 1's fresh UTC-pinned connection
— Postgres owns the instant. Not Django's session (it can hold a stale or rolled-back
`SET`), not a held connection (one opened before the first freeze never sees the later
database default), and not Python-side bookkeeping (that reports what we intended, so it
could never reveal a desync).

- [ ] **Step 6: implement per-test teardown**

The fixture is function-scoped, so its teardown runs per test. Release BOTH halves
unconditionally — passed, failed, or raised mid-apply — and release the DB GUC even if
stopping time-machine raises, since a stopped Python clock with a live DB GUC is the
deadlock direction. Use targeted `ALTER DATABASE <test_db> RESET absurd.fake_now`, never
`RESET ALL` (that would clobber unrelated database settings). Also `RESET` the
session-level GUC. A test that never froze must touch nothing.

- [ ] **Step 7: run the tests, confirm they pass**

Run: `uv run pytest tests/core/test_durable_clock.py -v --timeout=60` Expected: PASS, 15
tests.

Then confirm no GUC leaked:

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d postgres -tAc \
  "select datname, setconfig from pg_db_role_setting s join pg_database d on d.oid = s.setdatabase;"
```

Expected: no rows.

- [ ] **Step 8: gates + commit**

```bash
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git add pyproject.toml uv.lock django_absurd/test.py tests/tasks.py tests/core/test_durable_clock.py
git commit -m "feat(test): freeze and advance durable time"
```

---

## Task 4: leak recovery — session-start sweep and flush integration

Per-test teardown cannot save a run that was SIGKILLed mid-freeze: the GUC survives, and
with `--reuse-db` the next run's first draining test can hang before any resetting code
executes. So recovery must also happen BEFORE tests.

**Files:**

- Modify: `django_absurd/pytest_plugin.py`
- Modify: `django_absurd/flush.py` — `flush_absurd_state`, real signature
  `(*, drop_schema: bool = False) -> None`
- Modify: `tests/utils.py`
- Test: `tests/core/test_pytest_plugin.py`

**Interfaces:**

- Consumes: Task 3's reset helper.
- Produces: a session-scoped autouse sweep fixture; `flush_absurd_state` additionally
  resets the GUC; `tests/utils.py` gains `set_database_fake_now`,
  `read_database_fake_now`, `reset_database_fake_now`.

- [ ] **Step 1: write the failing tests**

Both behaviors need a real session boundary — a session fixture cannot be re-triggered
inside the session it already ran in, and "a failing test still releases the clock"
needs a test that genuinely fails. So the inner runs go through `pytester`.

Three things are required, all verified live. Do NOT write a conftest or ini into the
pytester directory — it is unnecessary and easy to get wrong:

1. `pytest_plugins = ["pytester"]` at the top of `tests/core/test_pytest_plugin.py`.
2. `monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))` so the inner interpreter can
   import `tests.core.settings`, with
   `REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]`. `DJANGO_SETTINGS_MODULE`
   needs no help — `runpytest_subprocess` inherits `os.environ`, and pytest-django
   exports it in the outer run.
3. `--reuse-db` on EVERY inner invocation. Without it the inner session tries to DROP
   and recreate the outer session's test database:
   `Got an error creating the test database: database "absurd_test_core" already exists`.
   Only the outer session's open connection prevents a real drop.

Each planting test needs `try/finally`. These tests MUST fail in step 2, and a stranded
`absurd.fake_now` would then poison every later test in the outer session, because every
new connection inherits it.

```python
def test_a_stranded_fake_now_is_swept_before_the_session_runs(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """A SIGKILLed run leaves the GUC behind; the next session must clear it."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    utils.set_database_fake_now("2036-01-01T00:00:00+00:00")
    try:
        pytester.makepyfile(
            inner_test="""
            import datetime as dt

            import pytest

            pytestmark = pytest.mark.django_db(transaction=True)


            def test_clock_is_real(absurd):
                assert absurd.now.year == dt.datetime.now(dt.UTC).year
            """
        )

        outcome = pytester.runpytest_subprocess("--reuse-db")

        outcome.assert_outcomes(passed=1)
        assert utils.read_database_fake_now() is None
    finally:
        utils.reset_database_fake_now()


def test_flush_absurd_state_resets_a_stranded_fake_now() -> None:
    utils.set_database_fake_now("2036-01-01T00:00:00+00:00")
    try:
        flush_absurd_state()

        assert utils.read_database_fake_now() is None
    finally:
        utils.reset_database_fake_now()


def test_a_test_that_raises_mid_advance_still_releases_the_clock(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """Proves recovery by SOME layer — fixture teardown or the flush reset. Both exist by
    now and both fire after the inner failing test; the sweep test above is the one that
    isolates the sweep, since it runs before any inner test."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    pytester.makepyfile(
        inner_test="""
        import datetime as dt

        import pytest

        pytestmark = pytest.mark.django_db(transaction=True)


        def test_advances_then_fails(absurd):
            absurd.advance(dt.timedelta(days=7))
            pytest.fail("deliberate")
        """
    )

    outcome = pytester.runpytest_subprocess("--reuse-db")

    outcome.assert_outcomes(failed=1)
    assert utils.read_database_fake_now() is None
```

Add three plain functions to `tests/utils.py` (no fixture — they need nothing beyond a
connection). `set_database_fake_now(value: str) -> None` and
`reset_database_fake_now() -> None` issue `ALTER DATABASE <test_db> SET` /
`RESET absurd.fake_now`; `read_database_fake_now() -> str | None` returns the stored
value by reading `pg_db_role_setting` joined to `pg_database`, or `None` when unset. All
three run on a short-lived autocommit connection with the statement composed via
`psycopg.sql` (`ALTER DATABASE` rejects bind parameters). Read the database name from
`connections["default"].settings_dict["NAME"]` so xdist workers hit their own database.

`tests/core/test_pytest_plugin.py` already imports `utils`, already imports
`flush_absurd_state` from `django_absurd.flush`, and already sets
`pytestmark = pytest.mark.django_db(transaction=True)`.

- [ ] **Step 2: run the tests, confirm they fail**

Run: `uv run pytest tests/core/test_pytest_plugin.py -v --timeout=120` Expected: FAIL —
the stranded GUC survives the inner session.

- [ ] **Step 3: implement the session-start sweep**

Add a session-scoped autouse fixture to `django_absurd/pytest_plugin.py`, mirroring
`_sweep_orphaned_pg_cron_jobs` exactly: take ONLY `request` so the guard runs before any
Django/DB fixture resolves, bail out when settings are unconfigured or no Absurd backend
is configured, then pull `django_db_setup` and `django_db_blocker` lazily via
`getfixturevalue`. That ordering is what keeps the plugin a clean no-op in a project
that merely has django-absurd installed. Issue the targeted `RESET` once. Read the
database name at runtime so xdist's per-worker databases are each swept.

`ALTER DATABASE ... RESET` works through Django's own connection while connected to that
same database (session-fixture time is autocommit, no transaction block), so no
dedicated connection is needed here.

Mirror whatever coverage treatment `_sweep_orphaned_pg_cron_jobs`' early-return arms
already get — the new sweep has the same unconfigured/no-backend arms, and patch
coverage will flag them otherwise.

- [ ] **Step 4: add the defensive reset to `flush_absurd_state`**

It lives in `django_absurd/flush.py:18`, NOT in `test.py` (which only imports it). Reset
the GUC there too, so the existing post-test flush recovers a leak even when the
fixture's own teardown never ran.

- [ ] **Step 5: run the tests, confirm they pass**

Run: `uv run pytest tests/core/test_pytest_plugin.py -v --timeout=120` Expected: PASS.

- [ ] **Step 6: confirm xdist safety**

Run: `uv run pytest tests/core -q --no-cov -n4 --timeout=120` Expected: all pass. Then
the `pg_db_role_setting` query from Task 3 returns no rows.

- [ ] **Step 7: gates + commit**

```bash
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git add django_absurd/pytest_plugin.py django_absurd/flush.py tests/utils.py tests/core/test_pytest_plugin.py
git commit -m "feat(test): sweep stranded fake_now at session start"
```

## Task 5: `emit`, the `absurd_drain_queue` delegate, and docs

**Files:**

- Modify: `django_absurd/test.py`, `django_absurd/pytest_plugin.py`
- Modify: `docs/web/testing.md`, `django_absurd/AGENTS.md`, `CLAUDE.md`
- Test: `tests/core/test_absurd_fixture.py`

**Interfaces:**

- Produces:
  `AbsurdTestRuntime.emit(name: str, payload: JsonValue | None = None, queue: str = "default") -> None`;
  `absurd_drain_queue` reimplemented as a delegate whose callable takes `queue` only.

- [ ] **Step 1: write the failing tests**

```python
def test_emit_resolves_a_waiting_task(absurd: AbsurdTestRuntime) -> None:
    call_command("absurd_sync_queues")
    result = tasks.sawait_event_once.enqueue("order.packed:via-fixture")
    assert [run.state for run in absurd.drain()] == ["sleeping"]

    absurd.emit("order.packed:via-fixture", {"tracking": "abc"})

    assert [run.state for run in absurd.drain()] == ["completed"]
    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.result == {"tracking": "abc"}


def test_absurd_drain_queue_alias_still_drains(
    absurd_drain_queue: t.Callable[..., None], absurd: AbsurdTestRuntime
) -> None:
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(2, 3)

    absurd_drain_queue()

    snapshot = absurd.get_result(result.id)
    assert snapshot is not None
    assert snapshot.state == "completed"
```

- [ ] **Step 2: run the tests, confirm they fail**

Run: `uv run pytest tests/core/test_absurd_fixture.py -v -k "emit or alias"` Expected:
FAIL — no `emit` attribute.

- [ ] **Step 3: implement `emit` and re-point the alias**

`emit` delegates to the existing public `django_absurd.events.emit_event`, defaulting
the queue. Reimplement `absurd_drain_queue` as a thin delegate to
`AbsurdTestRuntime.drain`, WITHOUT a `concurrency` parameter — the burst drain runs
lockstep batches rather than a rolling window, nothing in the repo passed it, and worker
concurrency stays covered through the CLI path.

- [ ] **Step 4: run the tests, confirm they pass**

Run: `uv run pytest tests/core -v --timeout=120` Expected: PASS.

- [ ] **Step 5: write the docs**

`docs/web/testing.md` — new section for the fixture, covering: the six members; that
`transaction=True` is required; freeze BEFORE enqueueing (freezing to a past instant
after rows exist leaves their deadlines in the DB's future); "install time-machine
yourself"; that durable time moves only on `freeze_at`/`advance`; the `TaskSnapshot`
caveats (`attempts` counts attempts CREATED, `sleeping` covers a retry backoff as well
as a durable sleep, `failure` is `None` mid-backoff — use the drain's `RunSnapshot` to
tell them apart); and three hazards: a `manage.py absurd_worker` subprocess is only
half-frozen, a savepoint rollback after an advance makes a later `enqueue()` stamp stale
time while `absurd.now` still looks correct, and advancing cannot make a **pg_cron**
schedule fire (its launcher runs in the central database on its own clock).

`django_absurd/AGENTS.md` — integration note in the testing section; update the
`absurd_drain_queue` entry, which currently documents a `concurrency` parameter.

`CLAUDE.md` — testing conventions: durable tests use the `absurd` fixture, never
`time.sleep`.

Cross-link per project convention (Absurd docs, specific Django docs).

- [ ] **Step 6: gates + commit**

```bash
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git add django_absurd/test.py django_absurd/pytest_plugin.py docs/web/testing.md django_absurd/AGENTS.md CLAUDE.md tests/core/test_absurd_fixture.py
git commit -m "feat(test): emit helper, drain alias, and docs"
```

---

## Done when

- `uvx --with tox-uv tox -e dev` green; `uv run pre-commit run --all-files` green.
- `uv run pytest tests/core -n4` green, and `pg_db_role_setting` empty afterwards.
- 100% patch coverage on the added lines.
- A 7-day sleep, an `await_event` timeout, a retry backoff, and a two-sleep chain are
  all tested without a single `time.sleep`.

Phase 2 then follows in its own plan: the freezegun→time-machine migration and the
existing- suite adoption recorded in the spec.
