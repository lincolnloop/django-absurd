# Durable Steps + Sleep Implementation Plan

> **SUPERSEDED (exposure layer)** — the durable context is now reached via
> `get_absurd_context()`/`aget_absurd_context()` accessors, not a `TaskContext`
> subclass. See the spec's "AMENDMENT" section. Do not execute the
> subclass/`takes_context` context-exposure steps below as written.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** expose Absurd's durable Steps + Sleep primitives to django-absurd task
functions, so a `takes_context=True` task can `step` (checkpoint/replay) and
`sleep_for`/`sleep_until` (durable suspend/resume).

**Architecture:** a new `django_absurd/context.py` defines two context classes extending
Django's `TaskContext` and wrapping the live Absurd SDK ctx — `AsyncAbsurdTaskContext`
(async tasks, methods delegate straight) and `AbsurdTaskContext` (sync tasks, methods
bridge to the worker loop via `run_coroutine_threadsafe`). `worker.build_task_context`
picks the variant by `inspect.iscoroutinefunction`; `build_handler` stops mislogging the
SDK's control-flow exceptions. Admin gains a Checkpoints inline under the task. Docs
teach the replay footguns; the web example gains a durable "wait task".

**Tech Stack:** Django 6 Tasks framework, `absurd_sdk` (`AsyncTaskContext`,
`begin_step`/`complete_step`, `SuspendTask`/`CancelledTask`/`FailedTask`), psycopg3,
asyncio.

> **TDD authoring rule (project):** each task shows the **failing test in full** and
> describes the **minimal implementation in prose** — never a finished production-code
> block. Signatures below are the interface contract, not the implementation.

## Global Constraints

- Runtime floor Django 6.0 / Python 3.12; psycopg3 backend only.
- `import typing as t`; `import datetime as dt`; absolute imports only; verb-named
  functions; no leading-underscore module constants/helpers; helpers below their caller.
- **Tests are ruff `ALL` + mypy strict too** — every test task function AND helper is
  fully annotated (params + return). **Django's `validate_task` hard-requires the first
  positional param of a `takes_context=True` task be literally named `context`** — use
  `context`, never `ctx`, as the param name (annotate it with the durable context type
  via a `if t.TYPE_CHECKING:` import + string annotation, so a RED step fails at runtime
  with `AttributeError`, not `ImportError`). `enqueue(absurd_spawn_params=…)` needs
  `# type: ignore[call-arg]` (matches every existing call site). No in-function imports
  (PLC0415) — top-level only. **No new ruff ignores/noqa.** Authorized mypy
  `type: ignore` (each needs an explanatory comment; stale django-stubs gaps, mypy-only,
  runtime-correct): `[call-arg]` on `enqueue(absurd_spawn_params=…)`; `[misc]` on the
  frozen/slots/kw_only `TaskContext` subclass (stubs omit `frozen=True` on the base);
  `[arg-type]` on each `@task(takes_context=True)` whose `context` param is annotated
  with the narrower durable context type (stubs type `@task` as
  `Concatenate[TaskContext, …]`). No other suppressions.
- Function-based pytest only; **no monkeypatch / unittest.mock / `responses` here**.
  `@pytest.mark.django_db(transaction=True)` for anything that runs the worker.
- Behavioral tests through real entrypoints (`enqueue` + `absurd_worker` burst); assert
  observable state via the SDK snapshot (`get_task_result(...).state`), never internals.
- Assert the COMPLETE stable message portion (up to any volatile tail); alphabetize
  `@pytest.mark.parametrize` values + fixture `params`.
- Full patch coverage (statement + branch) on added/changed lines.
- **ctx method names/args mirror the SDK verbatim:** `step(name, fn)`,
  `sleep_for(step_name, duration)`, `sleep_until(step_name, wake_at)`,
  `heartbeat(seconds=None)`, `headers` property; `run_step` on the sync variant only
  (SDK omits it on async — it can't work there).
- Django `TaskContext` is `@dataclass(frozen=True, slots=True, kw_only=True)` and NOT
  subscriptable at runtime → use the `TYPE_CHECKING` conditional-base alias, and make
  each subclass its own frozen/slots/kw_only dataclass.
- Docs teach **effectively-once** (persisted-once, executed at-least-once), NOT
  exactly-once.
- **Sleep timing recipe (avoids clock-skew flake):** durable-sleep test tasks sleep
  `1.5s`; tests `time.sleep(2)` between the suspending drain and the resuming drain.
  Wake is on the Python host clock; claim eligibility on the DB clock — thin margins
  flake under Docker-on-macOS drift.

## File Structure

- **Create** `django_absurd/context.py` — `AsyncAbsurdTaskContext`, `AbsurdTaskContext`,
  the `_TaskContextBase` conditional alias, the `R` TypeVar. One responsibility: durable
  context surface + the sync→loop bridge.
- **Modify** `django_absurd/worker.py` — `build_task_context` picks the variant;
  `build_handler` gains one control-flow-exception arm; module-level `LIFECYCLE_WORDS`.
- **Modify** `django_absurd/__init__.py` — re-export the two context classes.
- **Modify** `django_absurd/admin.py` — `available_at` into `RUN_INLINE_FIELDS`;
  `CHECKPOINT_INLINE_FIELDS` + `ReadOnlyCheckpointInline` + `build_checkpoint_inline`;
  wire into the tasks admin.
- **Modify** `django_absurd/admin_views.py` — checkpoints `task` relation +
  `search_fields`.
- **Create** `tests/worker_support.py` — shared `run_absurd_worker` + `get_task_result`
  (hoisted from `tests/core/test_worker.py`, which imports them from here). **Create**
  `tests/core/test_durable.py`; **add** durable test tasks to `tests/tasks.py` (sync) +
  `tests/atasks.py` (async).
- **Modify** `django_absurd/AGENTS.md`, `docs/web/` (new page + `zensical.toml` nav),
  `README.md`, `examples/web/app.py`.

---

### Task 1: Shared helpers + async durable context (`step`, `headers`, `heartbeat`)

**Files:**

- Create: `django_absurd/context.py`, `tests/worker_support.py`,
  `tests/core/test_durable.py`
- Modify: `django_absurd/worker.py` (`build_task_context`), `django_absurd/__init__.py`,
  `tests/core/test_worker.py` (import the hoisted helpers), `tests/atasks.py`
- Test: `tests/core/test_durable.py`

**Interfaces:**

- Produces: `django_absurd.context.AsyncAbsurdTaskContext` — a
  `@dataclass(frozen=True, slots=True, kw_only=True)` subclass of Django `TaskContext`,
  field `absurd_ctx: t.Any`; `headers` property (`-> Mapping[str, t.Any]`);
  `async def step(self, name: str, fn: Callable[[], Awaitable[R]]) -> R`;
  `async def heartbeat(self, seconds: int | None = None) -> None`.
- Produces: `tests/worker_support.py` `run_absurd_worker(queue="default") -> None` and
  `get_task_result(task_id, queue="default") -> TaskResultSnapshot | None`.
- Produces: `build_task_context` returns `AsyncAbsurdTaskContext` for a coroutine
  `task.func` (plain `TaskContext` otherwise — unchanged for now).

- [ ] **Step 1: Write the failing tests + shared helpers**

Create `tests/worker_support.py` (moved verbatim from `tests/core/test_worker.py:33-49`,
now the single source):

```python
import typing as t

import psycopg
from absurd_sdk import Absurd, TaskResultSnapshot
from django.core.management import call_command
from django.db import connections

from django_absurd.connection import register_jsonb_loader


def run_absurd_worker(queue: str = "default", concurrency: int = 1) -> None:
    call_command("absurd_worker", queue=queue, burst=True, concurrency=concurrency)


def get_task_result(
    task_id: t.Any, queue: str = "default"
) -> TaskResultSnapshot | None:
    raw_task_id = str(task_id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        return Absurd(conn).fetch_task_result(raw_task_id, queue)
    finally:
        conn.close()
```

In `tests/core/test_worker.py`, delete the local `run_absurd_worker`/`get_task_result`
defs and instead `from tests.worker_support import get_task_result, run_absurd_worker`.

Add async durable tasks to `tests/atasks.py` (module already imports `task`; add a
`TYPE_CHECKING` import for the annotation):

```python
import typing as t

if t.TYPE_CHECKING:
    from django_absurd.context import AsyncAbsurdTaskContext

DURABLE_STEP_CALLS: dict[str, int] = {"n": 0}


@task(takes_context=True)
async def astep_echo(context: "AsyncAbsurdTaskContext", value: str) -> str:
    async def compute() -> str:
        return value

    return await context.step("echo", compute)


@task(takes_context=True)
async def aheaders_tenant(context: "AsyncAbsurdTaskContext") -> str | None:
    return context.headers.get("tenant")


@task(takes_context=True)
async def aheartbeat_then_return(context: "AsyncAbsurdTaskContext", value: str) -> str:
    await context.heartbeat()
    return value
```

Create `tests/core/test_durable.py`:

```python
import pytest
from django.core.management import call_command

from django_absurd.params import AbsurdSpawnParams
from tests.atasks import aheaders_tenant, aheartbeat_then_return, astep_echo
from tests.worker_support import get_task_result, run_absurd_worker

pytestmark = pytest.mark.django_db(transaction=True)


def test_async_step_runs_and_returns_value() -> None:
    call_command("absurd_sync_queues")
    result = astep_echo.enqueue("hi")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.state == "completed"
    assert snap.result == "hi"


def test_async_headers_readable_from_ctx() -> None:
    call_command("absurd_sync_queues")
    result = aheaders_tenant.enqueue(  # type: ignore[call-arg]
        absurd_spawn_params=AbsurdSpawnParams(headers={"tenant": "acme"})
    )
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == "acme"


def test_async_heartbeat_is_callable() -> None:
    call_command("absurd_sync_queues")
    result = aheartbeat_then_return.enqueue("ok")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.state == "completed"
    assert snap.result == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_durable.py -v` Expected: FAIL — `astep_echo` gets a
plain `TaskContext` with no `.step` (`AttributeError`); `django_absurd.context` does not
exist.

- [ ] **Step 3: Implement (prose — minimal)**

Create `django_absurd/context.py`: a `TYPE_CHECKING` conditional-base alias
`_TaskContextBase` (`TaskContext[t.Any, t.Any]` under `TYPE_CHECKING`, else plain
`TaskContext` — mirror `admin.py:29-32`); `R = t.TypeVar("R")`. Define
`AsyncAbsurdTaskContext` as a `@dataclass(frozen=True, slots=True, kw_only=True)`
subclass adding `absurd_ctx: t.Any`; `headers` property returns
`self.absurd_ctx.headers`; `step`/`heartbeat` await the matching `self.absurd_ctx`
methods (`step(name, fn)` mirrors the SDK arg name). In `worker.build_task_context`,
build `task_result` as today, then return
`AsyncAbsurdTaskContext(task_result=…, absurd_ctx=ctx)` when
`inspect.iscoroutinefunction(task.func)`, else the plain `TaskContext`. Re-export
`AsyncAbsurdTaskContext` from `django_absurd/__init__.py`.

- [ ] **Step 4: Run the full core suite**

Run: `uv run pytest tests/core` Expected: PASS — the 3 new tests, and every existing
test (incl. `test_worker.py` using the hoisted helpers) still green.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/context.py django_absurd/worker.py django_absurd/__init__.py tests/worker_support.py tests/core/test_worker.py tests/atasks.py tests/core/test_durable.py
git commit -m "feat: expose async durable ctx (step/headers/heartbeat) to tasks"
```

---

### Task 2: Async `sleep_for`/`sleep_until` + control-flow logging fix

**Files:**

- Modify: `django_absurd/context.py` (`AsyncAbsurdTaskContext`),
  `django_absurd/worker.py` (`build_handler`, `LIFECYCLE_WORDS`), `tests/atasks.py`
- Test: `tests/core/test_durable.py`

**Interfaces:**

- Produces: `AsyncAbsurdTaskContext.sleep_for(step_name, duration)` /
  `sleep_until(step_name, wake_at)` (`wake_at: datetime | int | float`), delegating to
  `self.absurd_ctx`.
- Produces: `worker.LIFECYCLE_WORDS: dict[type, str]` =
  `{SuspendTask: "suspended", CancelledTask: "cancelled", FailedTask: "failed"}`;
  `build_handler` catches the trio in ONE arm (log `"django-absurd task {word}: …"` at
  INFO, then re-raise), before the generic `except Exception`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/atasks.py`:

```python
import time


@task(takes_context=True)
async def asleep_for_once(context: "AsyncAbsurdTaskContext", key: str) -> int:
    async def bump() -> int:
        DURABLE_STEP_CALLS["n"] += 1
        return DURABLE_STEP_CALLS["n"]

    n = await context.step("bump", bump)
    await context.sleep_for("nap", 1.5)
    return n


@task(takes_context=True)
async def asleep_until_once(context: "AsyncAbsurdTaskContext", key: str) -> str:
    await context.sleep_until("nap", time.time() + 1.5)
    return "woke"
```

Add to `tests/core/test_durable.py`:

```python
import logging
import time

from tests.atasks import (
    DURABLE_STEP_CALLS,
    asleep_for_once,
    asleep_until_once,
)


def test_async_sleep_for_suspends_then_resumes_replaying_step() -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    result = asleep_for_once.enqueue("k")

    run_absurd_worker()  # drain 1: bump runs, then sleep -> suspend
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    time.sleep(2)  # past wake (Python-clock wake vs DB-clock claim; wide margin)
    run_absurd_worker()  # drain 2: body replays, bump cached, completes
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == 1
    assert DURABLE_STEP_CALLS["n"] == 1  # step body ran once across the replay


def test_async_sleep_until_suspends_then_resumes() -> None:
    call_command("absurd_sync_queues")
    result = asleep_until_once.enqueue("k")
    run_absurd_worker()
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"
    time.sleep(2)
    run_absurd_worker()
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == "woke"


def test_suspend_logged_as_lifecycle_not_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    asleep_for_once.enqueue("k")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        run_absurd_worker()
    assert "django-absurd task suspended: name=tests.atasks.asleep_for_once" in caplog.text
    assert "task failed" not in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_durable.py -k "sleep or suspend" -v` Expected: FAIL
— no `sleep_for`/`sleep_until` on the context; and once reachable, `SuspendTask` hits
`except Exception` and is mislogged "task failed".

- [ ] **Step 3: Implement (prose — minimal)**

Add `sleep_for(step_name, duration)` + `sleep_until(step_name, wake_at)` to
`AsyncAbsurdTaskContext`, awaiting the matching `self.absurd_ctx` methods (delegate
directly; `sleep_for` does NOT route through our `sleep_until`). In `worker.py`, add
module-level
`LIFECYCLE_WORDS = {SuspendTask: "suspended", CancelledTask: "cancelled", FailedTask: "failed"}`
(import the three from `absurd_sdk`). In `build_handler`, add ONE arm before
`except Exception`: `except (SuspendTask, CancelledTask, FailedTask) as exc:` → log
`"django-absurd task %s: name=%s task_id=%s attempt=%d"` with
`LIFECYCLE_WORDS[type(exc)]` at INFO, then `raise` (SDK dispatch handles the trio as
control flow). Leave the generic `except Exception` ("task failed") intact.

- [ ] **Step 4: Run the full core suite**

Run: `uv run pytest tests/core` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/context.py django_absurd/worker.py tests/atasks.py tests/core/test_durable.py
git commit -m "feat: async durable sleep + single control-flow logging arm"
```

---

### Task 3: Sync durable context (loop bridge) — `step`/`sleep`/`heartbeat`/`headers`/`run_step`

**Files:**

- Modify: `django_absurd/context.py` (`AbsurdTaskContext`), `django_absurd/worker.py`
  (`build_task_context`), `django_absurd/__init__.py`, `tests/tasks.py`
- Test: `tests/core/test_durable.py`

**Interfaces:**

- Produces: `django_absurd.context.AbsurdTaskContext` — a
  `@dataclass(frozen=True, slots=True, kw_only=True)` `TaskContext` subclass, fields
  `absurd_ctx: t.Any` + `loop: asyncio.AbstractEventLoop`; sync methods
  `step(name, fn: Callable[[], R]) -> R`, `sleep_for(step_name, duration)`,
  `sleep_until(step_name, wake_at)`, `heartbeat(seconds=None)`, a `headers` property,
  and `run_step(name_or_fn=None)` (decorator, sync-only, all three SDK forms).
- Produces: `build_task_context` returns `AbsurdTaskContext` for a non-coroutine
  `takes_context` task, capturing `asyncio.get_running_loop()`.

- [ ] **Step 1: Write the failing tests**

Add sync durable tasks to `tests/tasks.py` (module already imports `task`; add the
`TYPE_CHECKING` import):

```python
import time
import typing as t

if t.TYPE_CHECKING:
    from django_absurd.context import AbsurdTaskContext

SYNC_STEP_CALLS: dict[str, int] = {"n": 0}


@task(takes_context=True)
def sstep_echo(context: "AbsurdTaskContext", value: str) -> str:
    return context.step("echo", lambda: value)


@task(takes_context=True)
def scoverage(context: "AbsurdTaskContext") -> dict[str, t.Any]:
    context.heartbeat()
    tenant = context.headers.get("tenant")

    @context.run_step
    def bare() -> str:
        return "bare-val"

    @context.run_step()
    def derived() -> str:
        return "derived-val"

    @context.run_step("custom")
    def named() -> str:
        return "named-val"

    return {"tenant": tenant, "bare": bare, "derived": derived, "named": named}


@task(takes_context=True)
def ssleep_for_once(context: "AbsurdTaskContext", key: str) -> int:
    def bump() -> int:
        SYNC_STEP_CALLS["n"] += 1
        return SYNC_STEP_CALLS["n"]

    n = context.step("bump", bump)
    context.sleep_for("nap", 1.5)
    return n


@task(takes_context=True)
def ssleep_until_once(context: "AbsurdTaskContext", key: str) -> str:
    context.sleep_until("nap", time.time() + 1.5)
    return "woke"
```

Add to `tests/core/test_durable.py`:

```python
from tests.tasks import (
    SYNC_STEP_CALLS,
    scoverage,
    ssleep_for_once,
    ssleep_until_once,
    sstep_echo,
)


def test_sync_step_runs_and_returns_value() -> None:
    call_command("absurd_sync_queues")
    result = sstep_echo.enqueue("hi")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == "hi"


def test_sync_headers_heartbeat_and_run_step_forms() -> None:
    call_command("absurd_sync_queues")
    result = scoverage.enqueue(  # type: ignore[call-arg]
        absurd_spawn_params=AbsurdSpawnParams(headers={"tenant": "acme"})
    )
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == {
        "bare": "bare-val",
        "derived": "derived-val",
        "named": "named-val",
        "tenant": "acme",
    }


def test_sync_sleep_for_suspends_then_resumes_replaying_step() -> None:
    call_command("absurd_sync_queues")
    SYNC_STEP_CALLS["n"] = 0
    result = ssleep_for_once.enqueue("k")

    run_absurd_worker()
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    time.sleep(2)
    run_absurd_worker()
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == 1
    assert SYNC_STEP_CALLS["n"] == 1


def test_sync_sleep_until_suspends_then_resumes() -> None:
    call_command("absurd_sync_queues")
    result = ssleep_until_once.enqueue("k")
    run_absurd_worker()
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"
    time.sleep(2)
    run_absurd_worker()
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == "woke"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_durable.py -k sync -v` Expected: FAIL — sync
`takes_context` tasks still get a plain `TaskContext`; no `.step`.

- [ ] **Step 3: Implement (prose — minimal)**

Add `AbsurdTaskContext` to `context.py`: same conditional base; fields `absurd_ctx` +
`loop`. Each async ctx op bridges with
`run_coroutine_threadsafe(coro, self.loop).result(timeout=…)` (generous timeout so a
mid-op worker shutdown can't hang the thread). `step`: bridge `begin_step(name)`; if the
handle is done return its state, else run `fn()` **in the current (executor) thread**,
then bridge `complete_step(handle, rv)`. `sleep_for(step_name, duration)` /
`sleep_until(step_name, wake_at)` / `heartbeat(seconds=None)`: bridge the matching async
ctx coroutine (`sleep`'s coroutine raises `SuspendTask`; `.result()` re-raises here →
propagates out through `call_sync`). `run_step(name_or_fn=None)`: mirror the SDK
(`absurd_sdk/__init__.py:734-764`) over `self.step` — bare `@ctx.run_step` (callable arg
→ step named by `fn.__name__`), `@ctx.run_step()` (derive name),
`@ctx.run_step("custom")`. `headers` property proxies `self.absurd_ctx.headers`. In
`build_task_context`, the non-coroutine branch now returns
`AbsurdTaskContext(task_result=…, absurd_ctx=ctx, loop=asyncio.get_running_loop())`.
Re-export `AbsurdTaskContext` from `__init__.py`.

- [ ] **Step 4: Run the full core suite**

Run: `uv run pytest tests/core` Expected: PASS (sync + async durable, plus existing sync
`takes_context` tasks unaffected).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/context.py django_absurd/worker.py django_absurd/__init__.py tests/tasks.py tests/core/test_durable.py
git commit -m "feat: sync durable ctx via loop bridge (step/sleep/run_step/heartbeat)"
```

---

### Task 4: Admin — Checkpoints inline + `available_at` on the Runs inline

**Files:**

- Modify: `django_absurd/admin_views.py` (`build_model_field`, checkpoints
  `search_fields`), `django_absurd/admin.py` (`RUN_INLINE_FIELDS`, checkpoint inline,
  tasks admin wiring)
- Test: `tests/core/test_admin/test_task.py`

**Interfaces:**

- Consumes: `tests.atasks.asleep_for_once` (Task 2) — its `bump` step + 1.5s sleep give
  a checkpoint row + a sleeping run with `available_at` set.
- Produces: a `Checkpoint` admin model with a `task` FK (attname `task_id`);
  `build_checkpoint_inline(checkpoint_model)`; the tasks admin inlines runs +
  checkpoints.

- [ ] **Step 1: Write the failing test**

Add to `tests/core/test_admin/test_task.py` (top-level import at the file head:
`from tests.atasks import DURABLE_STEP_CALLS, asleep_for_once`):

```python
def test_detail_inlines_checkpoints_and_run_available_at(
    client: Client, admin_user: User
) -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    asleep_for_once.enqueue("admin-k")
    call_command("absurd_worker", queue="default", burst=True)  # suspends
    client.force_login(admin_user)

    task = find_task("default", "tests.atasks.asleep_for_once")
    assert task is not None
    soup = parse_html(client.get(change_url(task.natural_key)))

    groups = soup.select(".inline-group")
    assert len(groups) >= 2  # runs + checkpoints
    assert soup.select_one('a[href*="/django_absurd/checkpoint/"]') is not None
    names = extract_field_texts(soup.select(".field-checkpoint_name"), "field-checkpoint_name")
    assert "bump" in {n for group in [names] for n in group} or (
        soup.select_one(".field-checkpoint_name") is not None
    )
    assert soup.select_one(".field-state") is not None  # cached step state renders
    available = soup.select_one(".field-available_at")
    assert available is not None
    assert available.get_text(strip=True) != ""  # sleeping run has a wake time
```

(`extract_field_texts` already exists in this file; if the `.field-checkpoint_name`
selector shape differs, assert on `soup.select_one(".field-checkpoint_name")` presence +
the checkpoint change-link — both prove the inline renders.)

- [ ] **Step 2: Run test to verify it fails**

Run:
`uv run pytest tests/core/test_admin/test_task.py::test_detail_inlines_checkpoints_and_run_available_at -v`
Expected: FAIL — task detail inlines runs only; no `.field-checkpoint_name`, and the run
inline has no `.field-available_at`.

- [ ] **Step 3: Implement (prose — minimal)**

In `admin_views.build_model_field`, add a branch for
`spec.name == "checkpoints" and col_name == "task_id"` returning a `task` FK exactly
like the runs branch (`to_field="task_id"`, `db_column="task_id"`,
`db_constraint=False`, `on_delete=DO_NOTHING`, `null=True`,
`related_name="checkpoints"`), and change the checkpoints `EntitySpec.search_fields`
`"task_id"` → `"task__task_id"`. In `admin.py`: add `"available_at"` to
`RUN_INLINE_FIELDS`; add `CHECKPOINT_INLINE_FIELDS` (`checkpoint_name`, `status`,
`state`, `updated_at`); add `ReadOnlyCheckpointInline` (mirror `ReadOnlyRunInline` —
`fk_name="task"`, read-only perms, `show_change_link`,
`ordering=("checkpoint_name",)`) + `build_checkpoint_inline(checkpoint_model)`; in
`build_entity_admin`'s `tasks` branch, build the checkpoint model and append its inline
to `extra["inlines"]`.

- [ ] **Step 4: Run the full core suite**

Run: `uv run pytest tests/core` Expected: PASS — new test + all existing admin tests
(`test_checkpoint.py`, `test_run.py`, `test_admin_models.py`, `test_orm_models.py`)
still green (the checkpoints FK is `db_constraint=False`; orphan `task_id` renders
safely).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin_views.py django_absurd/admin.py tests/core/test_admin/test_task.py
git commit -m "feat: admin inlines checkpoints + shows sleeping run available_at"
```

---

### Task 5: Docs + runnable example

**Files:**

- Modify: `django_absurd/AGENTS.md`, `README.md`, `examples/web/app.py`
- Create: `docs/web/durable-workflows.md`; Modify: `zensical.toml` (nav)

- [ ] **Step 1: Write AGENTS.md "Durable steps & sleep" section**

Both variants (sync no-`await`, async `await`); a `step` + `sleep_for` example; a
**typed** usage snippet (`async def workflow(context: AsyncAbsurdTaskContext, …)`)
showing autocomplete/mypy value; and the **full footgun set** as an explicit list: (a)
**effectively-once** — steps persist after `fn` returns on a separate connection,
executed at-least-once in a crash window, keep side effects idempotent; (b)
**deterministic naming/order** — step call order/names stable across replays; `step` and
`sleep` share one checkpoint namespace/counter; (c) **JSON-serializable step returns** —
persisted via `json.dumps`; `tuple` → `list` on replay; (d) **never swallow
`SuspendTask`** — re-raise it (and `CancelledTask`); (e) **long steps** — finish within
`claim_timeout` (default 120s) or call `context.heartbeat()`; (f) **absurd-only** —
`context.step` tasks fail under other Django backends; (g) sleep resume re-claims the
**same** run — attempt does not increment.

- [ ] **Step 2: Write the docs-site page + nav**

Create `docs/web/durable-workflows.md` mirroring the AGENTS.md section (may add
examples/links; must not contradict it). Add a `{"Durable workflows" = ...}` entry to
the `nav` in `zensical.toml`. Add a one-line link in `README.md` (no growth).

- [ ] **Step 3: Extend the web example with a wait-task**

In `examples/web/app.py` add a durable task (`step` + `sleep_for` ~5s) and a view/button
that enqueues it — demonstrating durable suspend/resume in the browser.

- [ ] **Step 4: Verify docs build + example runs**

Run: `uvx zensical build` Expected: `No issues found`. Then from `examples/web`:
`docker compose up -d --build`; confirm the worker logs the durable task suspending then
completing (bounded ≤30s per the examples convention); then `docker compose down -v`.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/AGENTS.md README.md docs/web/durable-workflows.md zensical.toml examples/web/app.py
git commit -m "docs: durable steps + sleep guide, site page, and web example"
```

---

## Self-Review

**Spec coverage:** step/sleep_for/sleep_until/heartbeat/headers (both) + run_step
(sync-only) → T1–T3; single control-flow arm covering the trio → T2; conditional-base +
frozen/slots subclass → T1/T3; admin Checkpoints inline + `available_at` + checkpoints
`search_fields` → T4; effectively-once + full footgun docs + typed snippet → T5; pinned
timing recipe + no-monkeypatch → T2/T3; example → T5.

**Coverage (100% patch):** async — step (done + replay-cached branches), sleep_for,
sleep_until, heartbeat, headers, the trio arm (via SuspendTask), both
`build_task_context` branches. sync — step (both branches), sleep_for, sleep_until,
heartbeat, headers, run_step (bare + derive-name + custom-name via `scoverage`). admin —
checkpoints `build_model_field` branch, `RUN_INLINE_FIELDS`, inline render path.

**Deviations from spec (deliberate):** trio logging is one arm with a word-map (not
three worded arms) — same observable text per type, one covered branch, no fragile
self-cancel/external-fail tests. Sync/async scenarios are separate one-off tests (not
`parametrize`) for readable failures. Both keep the behavior the spec requires.

**Type consistency:** `AsyncAbsurdTaskContext`/`AbsurdTaskContext` fields (`absurd_ctx`,
`loop`)

- SDK-verbatim signatures (`step(name, fn)`, `sleep_for(step_name, duration)`,
  `sleep_until(step_name, wake_at)`, `heartbeat(seconds=None)`, `headers`, sync-only
  `run_step`) consistent T1→T3. `build_task_context` variant choice is additive (sync
  stays plain `TaskContext` between T1 and T3 — no half-wired task).
