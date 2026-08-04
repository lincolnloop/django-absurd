# Durable-Primitive Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Log steps, step replays, durable sleeps, event waits and emits on
`django_absurd.context`, for sync and async tasks alike.

**Architecture:** Composition. A new `AsyncAbsurdTaskContext` wraps the SDK's
`AsyncTaskContext` and owns every log call; the existing sync `AbsurdTaskContext` bridge
delegates to that wrapper instead of the raw SDK context, so five of six primitives have
one implementation. `step` keeps two bodies (the sync one must run the user's `fn` off
the loop) sharing one message builder. Suspensions are classified by catching
`SuspendTask`, never by logging before the await.

**Tech Stack:** Django 6.0 / Python 3.12 floor, `absurd_sdk`, psycopg3, pytest +
`dj_absurd` fixture, mypy strict.

Spec:
[`docs/specs/2026-08-04-durable-primitive-logging.md`](../specs/2026-08-04-durable-primitive-logging.md).
Its "Probed, not assumed" section records what was verified against a live worker — do
not re-derive it.

## Global Constraints

- Branch `durable-primitive-logging`, already cut from `origin/main` at `a13e906`.
- **Every log record is plain text and ASCII in our own literals.** Caller strings (step
  names, event names) travel verbatim.
- **All these events log at INFO**, on `django_absurd.context` via
  `getLogger(__name__)`.
- **Assert the FULL rendered `record.getMessage()` with `==`.** Never a substring, never
  `in`, never a level-plus-count alone. Regex only where a message carries a
  nondeterministic duration, with the duration the only loose part.
- **Every event is driven from BOTH a sync and an async task.** The design's whole claim
  is that both share one implementation; unproven parity is not parity.
- **No new `noqa`, `type: ignore`, `pragma` or ruff ignore.** If one seems needed, STOP
  and report instead — it must be authorised first.
- Function-based pytest tests only. `import typing as t`, absolute imports, verb-named
  functions, helpers BELOW their callers, fixture params alphabetised, no comments
  narrating prior state.
- No monkeypatching. Drive real tasks through `dj_absurd`; freeze time with
  `dj_absurd.freeze_time()`, never `time.sleep`.
- 100% statement and branch coverage on added lines, through real entrypoints.
- Gates before every commit: `uv run pre-commit run --all-files` (owns ruff + mypy +
  prettier) and `uvx --with tox-uv tox -e dev` (all three suites). `git add` explicit
  paths first — `--all-files` silently skips untracked.
- Postgres must be up: `docker compose up -d db db_pg_cron`.
- Never write a person's name in a commit message or doc.

## Message formats

Fixed here so tasks agree. `task_id` is `ctx.task_id`; `name=` on step lines is
`handle.checkpoint_name` (the SDK numbers repeats — `dup`, `dup#2`).

```
step replayed: name=%s task_id=%s
step completed: name=%s task_id=%s duration=%.3fs
sleep suspended: step=%s task_id=%s for=%ss
sleep suspended: step=%s task_id=%s until=%s
sleep resumed: step=%s task_id=%s
event awaiting: name=%s task_id=%s timeout=%s
event received: name=%s task_id=%s
event emitted: name=%s task_id=%s
awaiting result: child=%s task_id=%s
```

## File structure

- `django_absurd/context.py` — everything. Gains a module logger, the
  `AsyncAbsurdTaskContext` wrapper, message builders, and a rewired sync bridge. Already
  the home of both accessors, so no new module: the wrappers and their accessors change
  together.
- `tests/core/test_logging_durable.py` — new, all assertions for these events.
- `tests/tasks.py` / `tests/atasks.py` — new fixture tasks for replay and events.
- `django_absurd/__init__.py` — export the new class.
- `django_absurd/AGENTS.md`, `docs/web/how-it-works.md`, `docs/UPSTREAM.md` — docs,
  Task 6.

---

### Task 1: The async wrapper, with step and replay logging

**Files:**

- Modify: `django_absurd/context.py`, `django_absurd/__init__.py`
- Create: `tests/core/test_logging_durable.py`
- Modify: `tests/atasks.py` (a task that replays a step)

**Interfaces:**

- Produces: `AsyncAbsurdTaskContext`, a frozen slots dataclass with field
  `absurd_ctx: AsyncTaskContext`, mirroring the SDK's async surface — `headers`
  (property), `step`, `begin_step`, `complete_step`, `sleep_for`, `sleep_until`,
  `await_event`, `emit_event`, `await_task_result`, `task_id` (property).
  `aget_absurd_context() -> AsyncAbsurdTaskContext`.
- Produces: `describe_step(checkpoint_name, task_id) -> str` and
  `describe_step_completed(checkpoint_name, task_id, duration) -> str`, the shared
  message builders both flavours' `step()` use.
- Consumes: nothing.

This task ships only `step`/`begin_step`/`complete_step` logging. Sleeps, events and
`await_task_result` are mirrored as plain delegation now and gain their lines in Tasks
3-5.

- [ ] **Step 1: Add an async fixture task that replays a step**

In `tests/atasks.py`, following the file's existing shape (module-level `@task`, verb
name): a task that runs one step named `"charge"` returning a constant, then raises on
its first attempt only — the raise driven by a module-level dict counter like
`DURABLE_STEP_CALLS` already in that file, NOT by a decorator or patching. Attempt 1
completes the step then fails; attempt 2 replays it and returns.

Give it `absurd_params(max_attempts=2)` at the call site in the test rather than baking
a ceiling into the fixture.

- [ ] **Step 2: Write the failing tests**

```python
import logging
import re

import pytest

from django_absurd import params as params_module
from django_absurd.test import AbsurdTestRuntime
from tests import atasks

pytestmark = pytest.mark.django_db(transaction=True)

DURATION = r"\d+\.\d{3}s"


def read_context_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == "django_absurd.context"
    ]


def test_an_async_step_logs_completed_with_a_duration(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = atasks.astep_echo.enqueue("v")
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert len(messages) == 1
    assert re.fullmatch(
        rf"step completed: name=echo task_id={task_id} duration={DURATION}",
        messages[0],
    )


def test_an_async_replayed_step_logs_replayed_not_completed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """Attempt 1 completes the step then fails; attempt 2 must skip the body."""
    two_attempts = params_module.absurd_params(max_attempts=2).bind(
        atasks.acharge_then_fail_once
    )
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = two_attempts.enqueue()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    completed = [m for m in messages if m.startswith("step completed: ")]
    assert len(completed) == 1
    assert re.fullmatch(
        rf"step completed: name=charge task_id={task_id} duration={DURATION}",
        completed[0],
    )
    assert messages.count(f"step replayed: name=charge task_id={task_id}") == 1
    assert len(messages) == 2
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/core/test_logging_durable.py -v` Expected: FAIL — no
`django_absurd.context` records exist, so both length assertions fail. Capture the
actual output; a failure for a different reason (import error, missing fixture task)
means fix that first.

- [ ] **Step 4: Implement the wrapper**

In prose, in `django_absurd/context.py`:

Add `logger = logging.getLogger(__name__)` at module level.

Add `AsyncAbsurdTaskContext`, a `@dataclass(frozen=True, slots=True)` holding
`absurd_ctx: AsyncTaskContext`, mirroring the SDK async surface named in Interfaces.
`task_id` and `headers` are properties delegating through. `sleep_for`, `sleep_until`,
`await_event`, `emit_event`, `await_task_result` are plain `async def` delegations for
now — no logging until Tasks 3-5.

`begin_step` awaits the SDK's, and when the returned handle is `done` logs the replay
line built from `handle.checkpoint_name` and `self.task_id`. `complete_step` is plain
delegation. `step` times a monotonic start, calls `self.begin_step(name)`, returns
`handle.state` when done (the replay line already emitted), otherwise awaits the user's
`fn`, completes through `self.complete_step`, logs the completed line, and returns.

Put the two message builders below the class, as the layout convention requires.

Change `aget_absurd_context()` to return `AsyncAbsurdTaskContext(absurd_ctx=…)` and
update its return annotation and docstring — it is no longer a passthrough. Export the
class from `django_absurd/__init__.py`'s `__all__`, alphabetised.

- [ ] **Step 5: Run to verify they pass, and that substitution did not break anything**

Run:
`uv run pytest tests/core/test_logging_durable.py tests/core/test_durable.py tests/core/test_async_worker.py -v`

Expected: PASS. `tests/core/test_durable.py` is the substitutability proof — its async
tasks call `aget_absurd_context()` and now receive the wrapper. If any of those fail,
the mirror is incomplete; add the missing member rather than narrowing the test.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/context.py django_absurd/__init__.py tests/core/test_logging_durable.py tests/atasks.py
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git commit -m "feat: log async step completion and replay"
```

---

### Task 2: The sync bridge delegates to the wrapper

**Files:**

- Modify: `django_absurd/context.py`
- Modify: `tests/core/test_logging_durable.py`
- Modify: `tests/tasks.py` (sync twin of the replay task)

**Interfaces:**

- Consumes: `AsyncAbsurdTaskContext`, `describe_step`, `describe_step_completed` from
  Task 1.
- Produces: `AbsurdTaskContext.async_ctx: AsyncAbsurdTaskContext` replacing its
  `absurd_ctx` field as the delegation target, with `absurd_ctx` kept as a property
  returning `self.async_ctx.absurd_ctx` so existing callers and tests keep working.

- [ ] **Step 1: Add the sync fixture task**

In `tests/tasks.py`, the sync twin of Task 1's async fixture: one step named `"charge"`,
fails on its first attempt only via a module-level counter dict (the file already has
`SYNC_STEP_CALLS`). Same shape, sync body.

- [ ] **Step 2: Write the failing tests**

```python
from tests import tasks


def test_a_sync_step_logs_completed_with_a_duration(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.sstep_echo.enqueue("v")
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert len(messages) == 1
    assert re.fullmatch(
        rf"step completed: name=echo task_id={task_id} duration={DURATION}",
        messages[0],
    )


def test_a_sync_replayed_step_logs_replayed_not_completed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    two_attempts = params_module.absurd_params(max_attempts=2).bind(
        tasks.scharge_then_fail_once
    )
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = two_attempts.enqueue()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert messages.count(f"step replayed: name=charge task_id={task_id}") == 1
    assert len([m for m in messages if m.startswith("step completed: ")]) == 1
    assert len(messages) == 2
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/core/test_logging_durable.py -v` Expected: the two new tests
FAIL with zero `django_absurd.context` records — the sync bridge still talks to the raw
SDK context. Task 1's async tests still PASS.

- [ ] **Step 4: Rewire the bridge**

In prose:

`AbsurdTaskContext`'s field becomes `async_ctx: AsyncAbsurdTaskContext`. Add an
`absurd_ctx` property returning `self.async_ctx.absurd_ctx`, because tests and user code
already reach through it (`tests/core/test_logging_lifecycle.py` does) — this keeps that
working without a second field.

Every delegating method changes target from `self.absurd_ctx.<op>` to
`self.async_ctx.<op>`, so each inherits the wrapper's log line exactly once. `step`
keeps its own body — it must run the user's `fn` in this executor thread between the
begin and complete bridges — but now calls `self.async_ctx.begin_step` /
`.complete_step`, so replay logging arrives for free, and logs the completed line itself
through `describe_step_completed`.

`get_absurd_context()` constructs
`AbsurdTaskContext(async_ctx=AsyncAbsurdTaskContext(...), loop=…)`.

- [ ] **Step 5: Run to verify**

Run:
`uv run pytest tests/core/test_logging_durable.py tests/core/test_durable.py tests/core/test_logging_lifecycle.py -v`
Expected: PASS, including the lifecycle file that reads `.absurd_ctx`.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/context.py tests/core/test_logging_durable.py tests/tasks.py
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git commit -m "feat: log sync step completion through the async wrapper"
```

---

### Task 3: Sleeps — suspended and resumed

**Files:**

- Modify: `django_absurd/context.py`
- Modify: `tests/core/test_logging_durable.py`

**Interfaces:**

- Consumes: `AsyncAbsurdTaskContext` from Task 1, the rewired bridge from Task 2.
- Produces: no new names. `sleep_for` and `sleep_until` gain logging inside the wrapper.

Existing fixture tasks cover this: `tests.tasks.ssleep_for_once`,
`tests.tasks.ssleep_until_once`, `tests.tasks.sleep_a_week`,
`tests.atasks.asleep_for_once`. No new fixtures needed.

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt


def test_an_async_sleep_logs_suspended_then_resumed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = atasks.asleep_for_once.enqueue("k")
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f"sleep suspended: step=nap task_id={task_id} for={tasks.WEEK_SECONDS}s"
        )
        == 1
    )
    assert messages.count(f"sleep resumed: step=nap task_id={task_id}") == 1


def test_a_sync_sleep_logs_suspended_then_resumed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = tasks.ssleep_for_once.enqueue("k")
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f"sleep suspended: step=nap task_id={task_id} for={tasks.WEEK_SECONDS}s"
        )
        == 1
    )
    assert messages.count(f"sleep resumed: step=nap task_id={task_id}") == 1


def test_a_sleep_until_reports_its_wake_time(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.ssleep_until_once.enqueue("k")
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    suspended = [
        m for m in read_context_messages(caplog) if m.startswith("sleep suspended: ")
    ]
    assert len(suspended) == 1
    assert re.fullmatch(
        rf"sleep suspended: step=nap task_id={task_id} until=\S+", suspended[0]
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_logging_durable.py -v` Expected: the three new tests
FAIL — no sleep lines emitted. Earlier tasks' tests PASS.

- [ ] **Step 3: Implement**

In prose, inside `AsyncAbsurdTaskContext`:

`sleep_for` and `sleep_until` wrap their delegation in `try`. Catch
`absurd_sdk.SuspendTask`, log the suspended line, and re-raise — the SDK recognises its
own class by identity, so swallowing it would change execution rather than logging.
After a normal return, log the resumed line.

This ordering is load-bearing and probe-verified: on resume the task body re-runs from
the top and the call returns from its checkpoint, so a line logged BEFORE the await
would fire on every attempt and claim a suspension that is not happening.

`sleep_for` reports `for=<duration>s` as passed; `sleep_until` reports
`until=<wake_at>`. Neither invents a format for the caller's value — interpolate what
was given.

- [ ] **Step 4: Run to verify**

Run: `uv run pytest tests/core/test_logging_durable.py tests/core/test_durable.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/context.py tests/core/test_logging_durable.py
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git commit -m "feat: log durable sleeps as suspended and resumed"
```

---

### Task 4: Events — awaiting, received, emitted

**Files:**

- Modify: `django_absurd/context.py`
- Modify: `tests/core/test_logging_durable.py`
- Modify: `tests/tasks.py`, `tests/atasks.py` (a waiter and an emitter, each flavour)

**Interfaces:**

- Consumes: `AsyncAbsurdTaskContext` from Task 1, the rewired bridge from Task 2.
- Produces: no new names. `await_event` and `emit_event` gain logging.

- [ ] **Step 1: Add waiter and emitter fixture tasks**

Four small tasks, following each file's existing shape: in `tests/tasks.py` a sync task
that awaits event `"probe.go"` with a timeout and returns its payload, and a sync task
that emits `"probe.go"` with a small payload; in `tests/atasks.py` the async twins. Name
them with verbs per the convention (they are defined in a shared fixture module, so a
terse adjective is not enough — these are reached module-qualified).

- [ ] **Step 2: Write the failing tests**

```python
def test_an_async_event_wait_logs_awaiting_then_received(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = atasks.await_the_probe_event.enqueue()
        dj_absurd.drain()
        atasks.emit_the_probe_event.enqueue()
        dj_absurd.drain()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f"event awaiting: name=probe.go task_id={task_id} timeout=3600"
        )
        == 1
    )
    assert messages.count(f"event received: name=probe.go task_id={task_id}") == 1
    assert len([m for m in messages if m.startswith("event emitted: ")]) == 1


def test_a_sync_event_wait_logs_awaiting_then_received(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.await_the_probe_event.enqueue()
        dj_absurd.drain()
        tasks.emit_the_probe_event.enqueue()
        dj_absurd.drain()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f"event awaiting: name=probe.go task_id={task_id} timeout=3600"
        )
        == 1
    )
    assert messages.count(f"event received: name=probe.go task_id={task_id}") == 1
```

The three `drain()` calls are the probe-verified shape: the first suspends the waiter,
the second runs the emitter, the third wakes the waiter.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/core/test_logging_durable.py -v` Expected: the two new tests
FAIL with no event lines. Earlier tasks' tests PASS.

- [ ] **Step 4: Implement**

In prose: `await_event` takes the same `try` / `except absurd_sdk.SuspendTask` /
re-raise shape as the sleeps — awaiting line in the except arm, received line after a
normal return. `emit_event` logs after the delegation returns, past tense, as the
convention requires.

- [ ] **Step 5: Run to verify**

Run:
`uv run pytest tests/core/test_logging_durable.py tests/core/test_durable.py tests/core/test_events.py -v`
Expected: PASS. `tests/core/test_events.py` already covers emit/await behaviour and must
stay green — it drives the same methods the wrapper now logs from.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/context.py tests/core/test_logging_durable.py tests/tasks.py tests/atasks.py
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git commit -m "feat: log event waits and emissions"
```

---

### Task 5: WITHDRAWN

`django_absurd/AGENTS.md:1244` already documents that `await_task_result` **is not
provided**, deliberately: the SDK's version polls and heartbeats inside a step rather
than suspending, so it holds the worker slot, and it is cross-queue-only. Users are
pointed at Django's `get_result()` / `aget_result()`.

The spec read its absence from the sync bridge as a gap on our side. It is a decision,
and the probe that "confirmed the gap" only confirmed the deliberate omission. Neither
wrapper exposes it, and no line logs it.

---

### Task 6: Docs, and the ASCII guard

**Files:**

- Modify: `django_absurd/AGENTS.md`, `docs/web/how-it-works.md`, `docs/UPSTREAM.md`
- Modify: `tests/core/test_logging_durable.py`

- [ ] **Step 1: Extend the non-ASCII guard to a step-driving task**

The shipped guard (`tests/core/test_logging_lifecycle.py`) drains a plain task, so it
never sees these new lines. Add one test to `test_logging_durable.py` that drains a task
using steps and a sleep, then asserts every `django_absurd.*` record's rendered
`getMessage().isascii()`. Scoped to text this package authors — a caller's own step and
event names travel verbatim, so use ASCII names in the fixture.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/core/test_logging_durable.py -v` Expected: PASS.

- [ ] **Step 3: Update the docs**

Invoke the `sync-docs` skill, then:

Both `django_absurd/AGENTS.md` and `docs/web/how-it-works.md` have a `Logging` section
listing what the `django_absurd` logger reports. Add durable-primitive coverage to the
existing sentence — steps, replays, sleeps, event waits — without adding an event
inventory. That section was deliberately tightened to what a user configures; keep it
that way, and mention `django_absurd.context` as the child logger to level if step lines
are too chatty.

In `docs/UPSTREAM.md`, add the asymmetry as an ask: the SDK's sync `TaskContext` has
`run_step`, its `AsyncTaskContext` does not, so our async wrapper cannot offer one
without inventing surface the SDK lacks. State what it retires.

- [ ] **Step 4: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/how-it-works.md docs/UPSTREAM.md tests/core/test_logging_durable.py
uv run pre-commit run --all-files
uvx --with tox-uv tox -e dev
git commit -m "docs: cover durable-primitive logging"
```

---

## Finishing

Do NOT push or open a PR. Local review flow: a `revdiff` pass against `origin/main`
after a fetch, then an adversarial review on the strongest available model. Merging is
the maintainer's call.
