# Absurd Lifecycle Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** django-absurd reports what Absurd is doing on its own plain-text loggers,
driven by the SDK's own hooks; emoji appears only in management-command console output.

**Architecture:** Two SDK hooks (`before_spawn`, `wrap_task_execution`) are passed to
both clients, each hook body contained so a logging fault can never fail a task.
Per-module loggers replace one flat name. The worker/beat commands attach a single plain
`StreamHandler` so their output is visible without a project `LOGGING` dict. No
formatter of ours, anywhere.

**Tech Stack:** absurd-sdk hooks, stdlib `logging`, Django management commands, pytest +
pytest-django + the project's `dj_absurd` fixture.

**Spec:**
[`docs/specs/2026-08-03-absurd-logger-design.md`](../specs/2026-08-03-absurd-logger-design.md).
Read it before Task 1 — it carries the reasoning, and two hook contracts that break task
execution rather than logging when got wrong.

## Global Constraints

- **Log records are plain text. Never put an emoji in one.** An un-encodable glyph
  raises `UnicodeEncodeError` inside `logging`, `StreamHandler.emit` swallows it into
  `handleError`, and the line is lost silently. Emoji belongs to `self.stdout` in
  commands only.
- **Every hook body is contained**: catch `Exception`, log it, continue. Both hooks run
  inside the SDK's `_execute_task` try, so an exception from ours is read as the TASK
  failing — attempt consumed, run marked failed, our bug recorded as the user's
  `failure_reason`, and nothing on stderr because the SDK has no logging.
- **`before_spawn` must return the spawn options** — the SDK assigns its return value. A
  hook returning `None` breaks every spawn.
- `import typing as t` — never `from typing import X`. Absolute imports only.
- Functions contain a verb. No leading-underscore module constants or helpers. Helpers
  BELOW their caller.
- Re-raise inside an `except` always chains `from exc`. Never `from None`.
- No new `noqa`, `type: ignore`, or ruff ignore. If you think you need one, STOP and
  ask.
- No `unittest.mock.patch`. No comments narrating prior state.
- Log messages are past tense, after the action.
- Tests: pytest, function-based only. Read [`tests/CLAUDE.md`](../../tests/CLAUDE.md)
  first. Type the fixture `dj_absurd: AbsurdTestRuntime`; alphabetize fixture params;
  add `@pytest.mark.django_db(transaction=True)` to anything that drains.
- 100% statement + branch coverage on added lines.
- Gates: `uv run pytest tests/core/<file> -v` while iterating, then
  `uvx --with tox-uv tox -e dev` and `uv run pre-commit run --all-files` before each
  commit. `git add` new files BEFORE the pre-commit gate — it skips untracked files.
- Postgres is already running on 5432/5434. Do NOT run `docker compose up`.
- A bare `ag PATTERN` with no path finds nothing in a `.claude/worktrees` checkout —
  always pass explicit paths.

---

### Task 1: Per-module loggers, prefixes dropped

**Files:**

- Modify: `django_absurd/worker.py:45`, `scheduler.py:19`, `deferred.py:29`,
  `dispatch.py:16`, `tasks.py:16` (the `getLogger("django_absurd")` calls and every
  message they format)
- Modify: `tests/core/test_signals_enqueue.py`, `test_signals_started.py` (they assert
  `record.name == "django_absurd"` and the `"django-absurd …"` message text)

**Interfaces:**

- Produces: loggers named `django_absurd.worker`, `.scheduler`, `.deferred`,
  `.dispatch`, `.tasks`. Later tasks add `.queues` and `.cleanup`. Parent
  `django_absurd` catches all.

- [ ] **Step 1: Write the failing test**

`tests/core/test_logging_names.py`:

```python
import logging

import pytest

from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils


@pytest.mark.django_db(transaction=True)
def test_worker_logs_under_its_own_module_logger(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time():
            tasks.add.enqueue(1, 2)
            dj_absurd.drain()

    names = {r.name for r in caplog.records if r.name.startswith("django_absurd")}
    assert names == {"django_absurd.worker"}


@pytest.mark.django_db(transaction=True)
def test_no_message_repeats_the_package_name(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """%(name)s already carries it; a hand-written prefix duplicates it."""
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time():
            tasks.add.enqueue(1, 2)
            dj_absurd.drain()

    ours = [r for r in caplog.records if r.name.startswith("django_absurd")]
    assert ours
    assert [r for r in ours if "django-absurd" in r.getMessage()] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_logging_names.py -v` Expected: FAIL — names is
`{"django_absurd"}`, and every message starts `"django-absurd "`.

- [ ] **Step 3: Rename the loggers and strip the prefixes**

In each of the five modules, replace `logging.getLogger("django_absurd")` with
`logging.getLogger(__name__)`, then remove the literal `"django-absurd "` from every
message that carries it. Keep the rest of each message and its `%s` args exactly as they
are — this task changes names, not content. Messages stay past tense.

- [ ] **Step 4: Update the unit-1 tests that assert the old name**

`tests/core/test_signals_enqueue.py` and `test_signals_started.py` filter on
`r.name == "django_absurd"` and assert a message beginning
`"django-absurd task_enqueued …"`. The containment log lives in `dispatch.py`, so the
name becomes `django_absurd.dispatch` and the message loses its prefix. Update both
filters and both full-text assertions.

- [ ] **Step 5: Run to verify pass**

Run:
`uv run pytest tests/core/test_logging_names.py tests/core/test_signals_enqueue.py tests/core/test_signals_started.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/ tests/core/
git commit -m "refactor: give each module its own logger"
```

---

### Task 2: The hook module, and spawn logging

**Files:**

- Create: `django_absurd/hooks.py`
- Create: `tests/core/test_logging_spawn.py`
- Modify: `django_absurd/connection.py:38-40` (`build_absurd_client`)
- Modify: `django_absurd/worker.py` (the `AsyncAbsurd(...)` construction in
  `aworker_client`)

**Interfaces:**

- Consumes: the per-module loggers from Task 1.
- Produces: `django_absurd.hooks.build_absurd_hooks() -> AbsurdHooks` — the hook dict
  passed to both clients. Task 3 adds `wrap_task_execution` to the same dict.

- [ ] **Step 1: Write the failing test**

```python
import logging

import pytest

from django_absurd.test import AbsurdTestRuntime
from tests import tasks


@pytest.mark.django_db(transaction=True)
def test_enqueue_logs_the_spawn_with_absurd_side_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="django_absurd"):
        tasks.add.enqueue(1, 2)

    spawns = [r for r in caplog.records if r.name == "django_absurd.hooks"]
    assert len(spawns) == 1
    assert spawns[0].levelno == logging.DEBUG
    message = spawns[0].getMessage()
    assert tasks.add.module_path in message
    assert "queue=default" in message


@pytest.mark.django_db(transaction=True)
def test_a_spawn_still_works_with_the_hook_attached(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """before_spawn's return value IS the spawn options; a hook returning None
    would break every spawn."""
    with dj_absurd.freeze_time():
        result = tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    assert dj_absurd.get_result(result.id).state == "completed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_logging_spawn.py -v` Expected: the first test FAILS
(no `django_absurd.hooks` records — we pass no hooks today); the second PASSES, and is a
guard that the return-value contract stays honoured.

- [ ] **Step 3: Write `django_absurd/hooks.py`**

In prose:

- Module docstring: the SDK's hooks are where Absurd's own lifecycle becomes observable,
  and every body is contained because the SDK reads an exception from a hook as the task
  failing.
- `logger = logging.getLogger(__name__)`.
- One public builder, `build_absurd_hooks()`, returning the SDK's `AbsurdHooks` dict.
- A `before_spawn` implementation that logs at DEBUG — the task name, the queue, the
  retry ceiling and the dedup key if present, i.e. the Absurd-side detail Django's own
  enqueue line omits — and then **returns the options it was given, unmodified**. Wrap
  the logging in `try/except Exception` and log the fault; the return must happen on
  both paths, so a logging failure still returns the options.
- Helpers below their caller; verb names.

- [ ] **Step 4: Pass the hooks to both clients**

`connection.py:build_absurd_client` constructs `Absurd(connections[using].connection)`,
and `worker.py:aworker_client` constructs `AsyncAbsurd(conn, queue_name=queue)`. Both
accept a `hooks=` keyword (verified: `absurd_sdk` client `__init__` at lines 1209, 1305,
1812). Pass `build_absurd_hooks()` to each.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/core/test_logging_spawn.py tests/core/test_enqueue.py -v`
Expected: PASS. `test_enqueue.py` is included because a broken `before_spawn` return
value breaks every enqueue in the suite, and this is the step that would reveal it.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/hooks.py django_absurd/connection.py django_absurd/worker.py tests/core/test_logging_spawn.py
git commit -m "feat: log Absurd spawns through the SDK's before_spawn hook"
```

---

### Task 3: Per-task logging moves to `wrap_task_execution`

**Files:**

- Modify: `django_absurd/hooks.py`
- Modify: `django_absurd/worker.py` (`build_handler` — remove its logging at lines ~366,
  400, 410, 422)
- Modify: `django_absurd/deferred.py:53` (delete the "waiting" line)
- Modify: `tests/core/test_logging_names.py` — its `names == {"django_absurd.worker"}`
  assertion breaks here: moving the per-task lines to `django_absurd.hooks` at INFO
  makes a plain drain emit both. Widen it to the set you actually observe, deliberately
  — that equality is a noise canary, so name every logger you expect rather than
  loosening it to a subset check.
- Create: `tests/core/test_logging_lifecycle.py`

**Interfaces:**

- Consumes: `build_absurd_hooks()` from Task 2.
- Produces: nothing new; the hook dict gains a `wrap_task_execution` entry.

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt
import logging

import pytest

from django_absurd import params as params_module
from django_absurd.test import AbsurdTestRuntime
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def read_hook_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "django_absurd.hooks"]


def test_a_successful_run_logs_started_then_completed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time():
            tasks.add.enqueue(1, 2)
            dj_absurd.drain()

    messages = read_hook_messages(caplog)
    assert len([m for m in messages if "started" in m]) == 1
    completed = [m for m in messages if "completed" in m]
    assert len(completed) == 1
    assert tasks.add.module_path in completed[0]


def test_a_suspended_run_logs_suspended_and_starts_again_on_wake(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time() as frozen_time:
            tasks.sleep_a_week.enqueue()
            dj_absurd.drain()
            frozen_time.shift(dt.timedelta(days=8))
            dj_absurd.drain()

    messages = read_hook_messages(caplog)
    assert len([m for m in messages if "suspended" in m]) == 1
    assert len([m for m in messages if "started" in m]) == 2
    assert len([m for m in messages if "completed" in m]) == 1


def test_a_retryable_failure_logs_error_with_the_attempt_count(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    two_attempts = params_module.absurd_params(max_attempts=2).bind(tasks.boom)
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time():
            two_attempts.enqueue()
            dj_absurd.drain()

    failures = [
        r
        for r in caplog.records
        if r.name == "django_absurd.hooks" and r.levelno == logging.ERROR
    ]
    assert len(failures) == 2
    assert failures[0].exc_info is not None
    assert "attempt=1" in failures[0].getMessage()
    assert "max_attempts=2" in failures[0].getMessage()
    assert "attempt=2" in failures[1].getMessage()


def test_the_deferred_wrapper_is_visible_without_its_own_log_line(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """The wrapper is a handler like any other, so the hook covers it."""
    from django.utils import timezone

    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time() as frozen_time:
            due = timezone.now() + dt.timedelta(hours=1)
            tasks.add.using(run_after=due).enqueue(1, 2)
            frozen_time.shift(dt.timedelta(hours=2))
            dj_absurd.drain()

    messages = read_hook_messages(caplog)
    assert [m for m in messages if ":run_after" in m]


def test_a_logging_fault_in_the_hook_does_not_fail_the_task(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """The SDK reads an exception from a hook as the TASK failing."""
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time():
            result = tasks.raises_in_repr.enqueue()
            dj_absurd.drain()

    assert dj_absurd.get_result(result.id).state == "completed"
```

The last test needs a task whose logged detail explodes when rendered. Add to
`tests/tasks.py` a task returning an object whose `__repr__` raises, and have the hook's
completed line include the return value's repr — OR, if the hook does not render the
return value, drive the fault another way and say so in your report. Do not weaken the
test to "the hook has a try/except"; the assertion must be that a real fault leaves the
task completed.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_logging_lifecycle.py -v` Expected: FAIL — no
`django_absurd.hooks` lifecycle records; today those lines come from
`django_absurd.worker` inside `build_handler`.

- [ ] **Step 3: Add `wrap_task_execution` to the hooks**

In prose:

- An async hook that receives `(ctx, execute)` and must call and return `execute()`.
- Read `attempt` and `max_attempts` off `ctx._task`; the task name is
  `ctx._task["task_name"]`, which for our tasks IS the dotted path.
- **Move `read_sdk_attempt` and `read_sdk_max_attempts` out of `worker.py` into
  `hooks.py`**, and have `worker.py` import them from there. NOT the other way:
  `worker.py` already imports `hooks` for `build_absurd_hooks()`, so `hooks` importing
  `worker` would be a cycle. The existing pre-authorized `# noqa: SLF001` travels with
  the function it annotates — a relocation, not a new ignore. Do not duplicate the
  readers.
- Log started before awaiting, then time the call: completed on success (with duration),
  suspended on `SuspendTask`, cancelled on `CancelledTask` (WARNING), already-failed on
  `FailedTask` (WARNING), failed on any other `Exception` (ERROR with `exc_info`,
  carrying attempt and max_attempts).
- **Re-raise every one of those SDK exceptions unchanged** — the SDK depends on
  receiving its own classes, and swallowing one would change execution.
- Contain the _logging_ only: a fault while logging must not propagate, but the task's
  own exception must. Structure it so those two are not confused.

- [ ] **Step 4: Remove the superseded logging**

Delete `build_handler`'s four log calls (`worker.py` ~366, 400, 410, 422) and
`deferred.py:53`. Leave `worker.py:185` (the worker-started line) alone — Task 5 owns
that. Check with an explicit-path sweep that no message text now has no emitter.

- [ ] **Step 5: Run to verify pass**

Run:
`uv run pytest tests/core/test_logging_lifecycle.py tests/core/test_logging_names.py tests/core/test_worker.py tests/core/test_durable.py tests/core/test_run_after.py -v`
Expected: PASS. The three existing files are included because they assert worker
behaviour around the code you just edited.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/hooks.py django_absurd/worker.py django_absurd/deferred.py tests/
git commit -m "feat: log the run lifecycle through wrap_task_execution"
```

---

### Task 4: The three missing log lines, and pg_cron's rename

**Files:**

- Modify: `django_absurd/queues.py` (`provision_backend`, `sync_queues` — add a logger)
- Modify: `django_absurd/cleanup.py` (`cleanup_queues` — add a logger)
- Modify: `django_absurd/worker.py` (`arun_worker` — add a stopped line; only `started`
  exists today, at `worker.py:185`)
- Modify: `django_absurd/pg_cron/signals.py`, `django_absurd/pg_cron/apps.py`,
  `tests/pg_cron/test_schedule_emission.py` (Step 0 — the rename Task 1 did not reach)
- Create: `tests/core/test_logging_maintenance.py`

**Interfaces:** none new.

- [ ] **Step 0: Finish the rename in pg_cron**

Task 1 renamed the five core modules; `django_absurd/pg_cron/` was never in this plan
and still has two flat loggers whose messages carry a `"django-absurd: "` prefix —
`pg_cron/signals.py:42` (message at `:102`) and `pg_cron/apps.py:23` (message at
`:125`). Once Task 5 attaches a handler to the parent `django_absurd` logger, those
lines print the package name twice.

Give both `logging.getLogger(__name__)` and strip the prefix, exactly as Task 1 did for
the core modules — same wording otherwise, same level, same args, ASCII only.
`tests/pg_cron/test_schedule_emission.py:62` asserts the prefixed text and must be
updated. That suite runs separately: `uv run pytest tests/pg_cron -v`.

- [ ] **Step 1: Write the failing tests**

```python
import logging

import pytest

from django_absurd import cleanup, queues
from django_absurd.management.base import resolve_backend

pytestmark = pytest.mark.django_db(transaction=True)


def test_provisioning_logs_what_it_created(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        queues.provision_backend(resolve_backend())

    records = [r for r in caplog.records if r.name == "django_absurd.queues"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


def test_cleanup_logs_what_it_removed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        cleanup.cleanup_queues()

    records = [r for r in caplog.records if r.name == "django_absurd.cleanup"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
```

Read both functions before writing the assertions — `provision_backend` returns a
`SyncResult` and `cleanup_queues` returns `list[QueueCleanup]`, so assert against what
they actually report (queue names created/reconciled; rows removed per queue). If
provisioning legitimately runs more than once in that call, assert the count you observe
rather than forcing 1.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_logging_maintenance.py -v` Expected: FAIL — neither
module has a logger today.

- [ ] **Step 3: Add a worker-stopped test**

`arun_worker` logs `started` inside `aworker_client`'s context but logs nothing when the
run ends, so a reader cannot tell a finished worker from a hung one. Add to the same
file:

```python
def test_the_worker_logs_when_it_stops(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        with dj_absurd.freeze_time():
            dj_absurd.drain()

    messages = [
        r.getMessage() for r in caplog.records if r.name == "django_absurd.worker"
    ]
    assert [m for m in messages if "started" in m]
    assert [m for m in messages if "stopped" in m]
```

Import `AbsurdTestRuntime` from `django_absurd.test`. Run it, watch the stopped
assertion fail, then log it past tense as the worker run ends — including on the burst
path, which is what `drain()` uses.

- [ ] **Step 4: Add the two maintenance lines**

`logging.getLogger(__name__)` in each module. Log after the work, past tense, reporting
what the return value says: which queues were created or reconciled; how many rows
cleanup removed from which queues. INFO. If nothing happened, say that rather than
logging an empty list.

- [ ] **Step 5: Run to verify pass**

Run:
`uv run pytest tests/core/test_logging_maintenance.py tests/core/test_queue_sync.py tests/core/test_cleanup.py tests/core/test_worker.py -v`
then `uv run pytest tests/pg_cron -v` for Step 0. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/ tests/
git commit -m "feat: log worker shutdown, queue provisioning and cleanup"
```

---

### Task 5: The worker and beat commands make their logs visible

**Files:**

- Modify: `django_absurd/management/commands/absurd_worker.py`, `absurd_beat.py`
- Create: `django_absurd/logging.py` (the attach helper)
- Create: `tests/core/test_logging_handler.py`

**Interfaces:**

- Produces: `django_absurd.logging.attach_console_handler() -> None` — idempotent;
  attaches one plain `StreamHandler` at INFO to the `django_absurd` logger, and does
  nothing when the project has already configured something that would catch those
  records.

- [ ] **Step 1: Write the failing tests**

```python
import logging

import pytest

from django_absurd import logging as absurd_logging


def test_importing_the_package_attaches_nothing() -> None:
    """A library must not fight the project's LOGGING."""
    import django_absurd

    assert logging.getLogger("django_absurd").handlers == []


def test_attaching_gives_the_package_logger_one_info_handler() -> None:
    logger = logging.getLogger("django_absurd")
    try:
        absurd_logging.attach_console_handler()
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.StreamHandler)
        assert logger.level == logging.INFO
    finally:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)


def test_attaching_twice_does_not_duplicate_the_handler() -> None:
    logger = logging.getLogger("django_absurd")
    try:
        absurd_logging.attach_console_handler()
        absurd_logging.attach_console_handler()
        assert len(logger.handlers) == 1
    finally:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)


def test_attaching_defers_to_a_handler_the_project_configured(
    settings: object,
) -> None:
    logger = logging.getLogger("django_absurd")
    configured = logging.NullHandler()
    logger.addHandler(configured)
    try:
        absurd_logging.attach_console_handler()
        assert logger.handlers == [configured]
    finally:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_logging_handler.py -v` Expected: the import test
PASSES (nothing attaches today, and it must stay that way); the other three FAIL on the
missing module.

- [ ] **Step 3: Write `django_absurd/logging.py`**

In prose: one public function, `attach_console_handler()`. It returns immediately if the
`django_absurd` logger already has any handler — that is the project's configuration and
we defer to it. Otherwise attach a single `logging.StreamHandler` and set the logger's
level to INFO. No `Formatter` of ours: the default one is fine, and a formatter is the
thing this spec deliberately does not ship. Idempotent.

Note the module is named `logging.py` inside the package; because the project uses
absolute imports only, `import logging` inside it still resolves to the stdlib. Confirm
that by running the tests rather than reasoning about it.

- [ ] **Step 4: Call it from both commands**

`absurd_worker.handle` and `absurd_beat.handle` call `attach_console_handler()` before
starting work. Nowhere else — not `AppConfig.ready`, not module import, not the other
commands.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/core/test_logging_handler.py tests/core/test_worker.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/logging.py django_absurd/management/ tests/core/test_logging_handler.py
git commit -m "feat: make worker and beat logs visible without a LOGGING dict"
```

---

### Task 6: Emoji in command output

**Files:**

- Modify: `django_absurd/management/commands/absurd_worker.py:109` (the started banner)
  and its stop path, `absurd_beat.py`, `absurd_sync_queues.py`
- Create: `tests/core/test_command_output.py`

**Interfaces:** none new.

- [ ] **Step 1: Write the failing tests**

```python
import io

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db(transaction=True)


def test_sync_queues_decorates_its_console_output() -> None:
    out = io.StringIO()
    call_command("absurd_sync_queues", stdout=out)

    assert "🗃️" in out.getvalue()


def test_the_worker_banner_carries_the_elephant(dj_absurd: object) -> None:
    out = io.StringIO()
    call_command("absurd_worker", burst=True, stdout=out)

    assert "🐘" in out.getvalue()
```

Check `absurd_worker`'s existing arguments before writing the second test — it is
invoked elsewhere in the suite as
`call_command("absurd_worker", queue=..., burst=True, ...)` (see
`tests/utils.py::run_absurd_worker`); match that shape.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_command_output.py -v` Expected: FAIL — no glyphs in
either output.

- [ ] **Step 3: Decorate the command writes**

Prepend 🐘 to the worker's start and stop banners, 🗃️ to what `absurd_sync_queues`
reports, and 🥁 to the beat's start line. Write through `self.stdout` as those commands
already do; where a line is already styled via `self.style`, keep the style and add the
glyph.

**Only command output.** No emoji reaches a log record — that is the constraint this
whole unit is shaped around.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_command_output.py tests/core/test_queue_sync.py -v`
Expected: PASS.

- [ ] **Step 5: Assert the constraint holds**

Add one test, in the same file, that drains a task and asserts no `django_absurd` log
record contains a non-ASCII character. This is the guard that keeps decoration out of
records, where an un-encodable glyph would drop the line.

- [ ] **Step 6: Commit**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
git add django_absurd/management/ tests/core/test_command_output.py
git commit -m "feat: decorate command output with emoji"
```

---

### Task 7: Documentation

**Files:**

- Modify: `django_absurd/AGENTS.md`, `docs/web/how-it-works.md`

- [ ] **Step 1: Invoke the docs skill**

Run the `sync-docs` skill.

- [ ] **Step 2: Cover only the knobs**

Two, both small:

- The logger names — `django_absurd` and its per-module children — so a project can
  route or level them in its own `LOGGING`, including silencing just the beat.
- `absurd_worker` and `absurd_beat` attach a plain INFO handler when the project has
  configured none, so their lines are visible out of the box.

Say that log records are plain text and the emoji lives in command output. Do NOT tour
the event vocabulary — the log speaks for itself — and do not promise the next unit's
durable- primitive logging.

- [ ] **Step 3: Commit**

```bash
uv run pre-commit run --all-files
git add -A
git commit -m "docs: document the django_absurd loggers"
```

---

## Finishing

Do NOT push or open a PR. Local review flow: `revdiff` against `origin/main` after a
fetch, then an adversarial review. Merging is the maintainer's call.
