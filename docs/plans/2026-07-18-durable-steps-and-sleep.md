# Durable Steps + Sleep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** expose Absurd's durable Steps + Sleep primitives to django-absurd task
functions, so a `takes_context=True` task can `step` (checkpoint/replay) and
`sleep_for`/`sleep_until` (durable suspend/resume).

**Architecture:** a new `django_absurd/context.py` defines two context classes extending
Django's `TaskContext` and wrapping the live Absurd SDK ctx — `AsyncDurableContext`
(async tasks, methods delegate straight) and `DurableContext` (sync tasks, methods
bridge to the worker loop via `run_coroutine_threadsafe`). `worker.build_task_context`
picks the variant by `inspect.iscoroutinefunction`; `build_handler` stops mislogging the
SDK's control-flow exceptions. Admin gains a Checkpoints inline under the task. Docs
teach the replay footguns; the web example gains a durable "wait task".

**Tech Stack:** Django 6 Tasks framework, `absurd_sdk` (`AsyncTaskContext`,
`begin_step`/`complete_step`, `SuspendTask`/`CancelledTask`/`FailedTask`), psycopg3,
asyncio.

> **TDD authoring rule (project):** these tasks show the **failing test in full** and
> describe the **minimal implementation in prose** — never a finished production-code
> block. Signatures below are the interface contract, not the implementation.

## Global Constraints

- Runtime floor Django 6.0 / Python 3.12; psycopg3 backend only.
- `import typing as t`; `import datetime as dt`; absolute imports only; verb-named
  functions; no leading-underscore module constants/helpers; helpers below their caller.
- mypy strict (explicit `t.Any` allowed); ruff ANN + E501 enforced in tests.
- Function-based pytest only; **no monkeypatch / unittest.mock / `responses` here**.
  `@pytest.mark.django_db(transaction=True)` for anything that runs the worker
  (commits + reschedule + resume).
- Behavioral tests through real entrypoints (`enqueue` + `absurd_worker` burst); assert
  observable state via the SDK snapshot (`get_task_result(...).state`) — never
  internals.
- Assert the COMPLETE message text where a message is asserted; alphabetize
  `@pytest.mark.parametrize` values + fixture `params`.
- Full patch coverage (statement + branch) on added/changed lines.
- **ctx method names/args mirror the SDK 1:1** — `step(name, fn)`,
  `sleep_for(name, seconds)`, `sleep_until(name, when)`, `heartbeat(seconds=None)`,
  `headers` property; `run_step` on the sync variant only (SDK omits it on async).
- Django `TaskContext` is `@dataclass(frozen=True, slots=True, kw_only=True)` and is NOT
  subscriptable at runtime → use the `TYPE_CHECKING` conditional-base alias, and make
  each subclass its own frozen/slots/kw_only dataclass.
- Docs teach **effectively-once** (persisted-once, executed at-least-once), NOT
  exactly-once.

## File Structure

- **Create** `django_absurd/context.py` — `AsyncDurableContext`, `DurableContext`, the
  `_TaskContextBase` conditional alias, the `R` TypeVar. One responsibility: the durable
  context surface + the sync→loop bridge.
- **Modify** `django_absurd/worker.py` — `build_task_context` picks the variant;
  `build_handler` gains control-flow-exception arms.
- **Modify** `django_absurd/__init__.py` — re-export the two context classes.
- **Modify** `django_absurd/admin.py` — `available_at` into `RUN_INLINE_FIELDS`;
  `CHECKPOINT_INLINE_FIELDS` + `ReadOnlyCheckpointInline` + `build_checkpoint_inline`;
  wire the checkpoints inline into the tasks admin.
- **Modify** `django_absurd/admin_views.py` — checkpoints `task` relation +
  `search_fields`.
- **Create** `tests/core/test_durable.py`; **add** durable test tasks to
  `tests/tasks.py` (sync) + `tests/atasks.py` (async).
- **Modify** `django_absurd/AGENTS.md`, `docs/web/` (new page + `zensical.toml` nav),
  `README.md`, `examples/web/app.py`.

---

### Task 1: Async durable context — `step`, `headers`, `heartbeat`

**Files:**

- Create: `django_absurd/context.py`
- Modify: `django_absurd/worker.py` (`build_task_context`), `django_absurd/__init__.py`
- Add tasks: `tests/atasks.py`
- Test: `tests/core/test_durable.py`

**Interfaces:**

- Produces: `django_absurd.context.AsyncDurableContext` — a
  `@dataclass(frozen=True, slots=True, kw_only=True)` subclass of Django `TaskContext`
  with field `absurd_ctx: t.Any`, a `headers` property (`-> Mapping[str, t.Any]`),
  `async def step(self, name: str, fn: Callable[[], Awaitable[R]]) -> R`, and
  `async def heartbeat(self, seconds: int | None = None) -> None`.
- Produces: `build_task_context(task, ctx, args, kwargs)` returns `AsyncDurableContext`
  for a coroutine `task.func` (plain `TaskContext` otherwise, unchanged for now).
- Consumes: the live SDK `AsyncTaskContext` passed to the handler as `ctx`.

- [ ] **Step 1: Write the failing tests**

Add async durable tasks to `tests/atasks.py`:

```python
from django.tasks import task

DURABLE_STEP_CALLS: dict[str, int] = {"n": 0}


@task(takes_context=True)
async def astep_echo(ctx, value):
    async def compute():
        return value

    return await ctx.step("echo", compute)


@task(takes_context=True)
async def aheaders_tenant(ctx):
    return ctx.headers.get("tenant")


@task(takes_context=True)
async def aheartbeat_then_return(ctx, value):
    await ctx.heartbeat()
    return value
```

Create `tests/core/test_durable.py`:

```python
import pytest
from absurd_sdk import Absurd, TaskResultSnapshot
import psycopg
from django.core.management import call_command
from django.db import connections

from django_absurd.connection import register_jsonb_loader
from django_absurd.params import AbsurdSpawnParams
from tests.atasks import aheaders_tenant, aheartbeat_then_return, astep_echo

pytestmark = pytest.mark.django_db(transaction=True)


def run_absurd_worker(queue: str = "default") -> None:
    call_command("absurd_worker", queue=queue, burst=True)


def get_task_result(task_id, queue: str = "default") -> TaskResultSnapshot | None:
    raw = str(task_id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        return Absurd(conn).fetch_task_result(raw, queue)
    finally:
        conn.close()


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
    result = aheaders_tenant.enqueue(
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

Run: `uv run pytest tests/core/test_durable.py -v` Expected: FAIL — `astep_echo`
currently receives a plain `TaskContext` with no `.step` (`AttributeError`), and
`django_absurd.context` does not exist.

- [ ] **Step 3: Implement (prose — minimal)**

Create `django_absurd/context.py`: a `TYPE_CHECKING` conditional-base alias
`_TaskContextBase = TaskContext[t.Any, t.Any]` under `TYPE_CHECKING` else plain
`TaskContext` (mirroring `admin.py:29-32`); an `R = t.TypeVar("R")`. Define
`AsyncDurableContext` as a `@dataclass(frozen=True, slots=True, kw_only=True)` subclass
adding the `absurd_ctx: t.Any` field; `headers` property returns
`self.absurd_ctx.headers`; `step`/`heartbeat` await the matching `self.absurd_ctx`
methods. In `worker.build_task_context`, keep building `task_result` as today, then
return `AsyncDurableContext(task_result=..., absurd_ctx=ctx)` when
`inspect.iscoroutinefunction(task.func)`, else the plain `TaskContext` as now. Re-export
`AsyncDurableContext` from `django_absurd/__init__.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_durable.py -v` Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/context.py django_absurd/worker.py django_absurd/__init__.py tests/atasks.py tests/core/test_durable.py
git commit -m "feat: expose async durable ctx (step/headers/heartbeat) to tasks"
```

---

### Task 2: Async `sleep_for`/`sleep_until` + control-flow logging fix

**Files:**

- Modify: `django_absurd/context.py` (`AsyncDurableContext`), `django_absurd/worker.py`
  (`build_handler`)
- Add tasks: `tests/atasks.py`
- Test: `tests/core/test_durable.py`

**Interfaces:**

- Produces: `AsyncDurableContext.sleep_for(name, seconds)` / `sleep_until(name, when)`
  (`when: datetime | int | float`), delegating to `self.absurd_ctx`.
- Produces: `build_handler` catches `SuspendTask` (log "suspended"), `CancelledTask`
  (log "cancelled"), `FailedTask` (log "failed (explicit)"), each **re-raised**, before
  the generic `except Exception` (log "failed").

- [ ] **Step 1: Write the failing tests**

Add to `tests/atasks.py`:

```python
@task(takes_context=True)
async def asleep_once(ctx, key):
    async def bump():
        DURABLE_STEP_CALLS["n"] += 1
        return DURABLE_STEP_CALLS["n"]

    n = await ctx.step("bump", bump)
    await ctx.sleep_for("nap", 0.6)
    return n
```

Add to `tests/core/test_durable.py`:

```python
import logging
import time

from tests.atasks import DURABLE_STEP_CALLS, asleep_once


def test_async_sleep_suspends_then_resumes_replaying_step() -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    result = asleep_once.enqueue("k")

    run_absurd_worker()  # drain 1: run bump, then sleep -> suspend
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    time.sleep(0.7)  # wall-clock past wake (fake_now is DB-side only; no monkeypatch)
    run_absurd_worker()  # drain 2: body replays, bump cached, completes
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == 1  # bump's cached value
    assert DURABLE_STEP_CALLS["n"] == 1  # step body executed once across the replay


def test_suspend_logged_as_lifecycle_not_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    asleep_once.enqueue("k")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        run_absurd_worker()
    assert "suspended" in caplog.text
    assert "task failed" not in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_durable.py -k "sleep or suspend" -v` Expected: FAIL
— no `sleep_for` on the context; and once reachable, `SuspendTask` is mislogged "task
failed" and the second assertion trips.

- [ ] **Step 3: Implement (prose — minimal)**

Add `sleep_for`/`sleep_until` to `AsyncDurableContext`, awaiting the matching
`self.absurd_ctx` methods (`sleep_for` computes nothing itself — delegate directly). In
`worker.build_handler`, import `SuspendTask`, `CancelledTask`, `FailedTask` from
`absurd_sdk` and add three `except` arms **before** the existing `except Exception`: log
suspend/cancel at INFO, explicit-fail at WARNING, and re-raise each so the SDK dispatch
loop handles them (it treats the trio as control flow). Leave the generic
`except Exception` (crash → "task failed") intact.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_durable.py -v` Expected: PASS (all async tests).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/context.py django_absurd/worker.py tests/atasks.py tests/core/test_durable.py
git commit -m "feat: async durable sleep + fix control-flow exception logging"
```

---

### Task 3: Sync durable context (loop bridge) — `step`/`sleep`/`heartbeat`/`headers`/`run_step`

**Files:**

- Modify: `django_absurd/context.py` (`DurableContext`), `django_absurd/worker.py`
  (`build_task_context`), `django_absurd/__init__.py`
- Add tasks: `tests/tasks.py`
- Test: `tests/core/test_durable.py`

**Interfaces:**

- Produces: `django_absurd.context.DurableContext` — a
  `@dataclass(frozen=True, slots=True, kw_only=True)` `TaskContext` subclass with fields
  `absurd_ctx: t.Any` and `loop: asyncio.AbstractEventLoop`; sync methods
  `step(name, fn: Callable[[], R]) -> R`, `sleep_for(name, seconds)`,
  `sleep_until(name, when)`, `heartbeat(seconds=None)`, a `headers` property, and
  `run_step(name_or_fn=None)` (decorator, sync-only, mirrors the SDK).
- Produces: `build_task_context` returns `DurableContext` for a non-coroutine
  `takes_context` task, capturing `asyncio.get_running_loop()`.

- [ ] **Step 1: Write the failing tests**

Add sync durable tasks to `tests/tasks.py`:

```python
from django.tasks import task

SYNC_STEP_CALLS: dict[str, int] = {"n": 0}


@task(takes_context=True)
def sstep_echo(ctx, value):
    return ctx.step("echo", lambda: value)


@task(takes_context=True)
def srun_step_echo(ctx, value):
    @ctx.run_step("echo")
    def echo():
        return value

    return echo  # run_step rebinds `echo` to the step's return value


@task(takes_context=True)
def ssleep_once(ctx, key):
    def bump():
        SYNC_STEP_CALLS["n"] += 1
        return SYNC_STEP_CALLS["n"]

    n = ctx.step("bump", bump)
    ctx.sleep_for("nap", 0.6)
    return n
```

Add to `tests/core/test_durable.py`:

```python
from tests.tasks import SYNC_STEP_CALLS, srun_step_echo, ssleep_once, sstep_echo


def test_sync_step_runs_and_returns_value() -> None:
    call_command("absurd_sync_queues")
    result = sstep_echo.enqueue("hi")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == "hi"


def test_sync_run_step_decorator_rebinds_to_result() -> None:
    call_command("absurd_sync_queues")
    result = srun_step_echo.enqueue("deco")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == "deco"


def test_sync_sleep_suspends_then_resumes_replaying_step() -> None:
    call_command("absurd_sync_queues")
    SYNC_STEP_CALLS["n"] = 0
    result = ssleep_once.enqueue("k")

    run_absurd_worker()
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    time.sleep(0.7)
    run_absurd_worker()
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == 1
    assert SYNC_STEP_CALLS["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_durable.py -k sync -v` Expected: FAIL — sync
`takes_context` tasks still get a plain `TaskContext`; no `.step`.

- [ ] **Step 3: Implement (prose — minimal)**

Add `DurableContext` to `context.py`: same conditional base; fields `absurd_ctx` +
`loop`. Each method bridges to the loop with
`run_coroutine_threadsafe(coro, self.loop).result()`. `step`: bridge `begin_step(name)`;
if the handle is done return its state, else run `fn()` **in the current (executor)
thread**, then bridge `complete_step(handle, rv)`.
`sleep_for`/`sleep_until`/`heartbeat`: bridge the matching async ctx coroutine
(`sleep`'s coroutine raises `SuspendTask`, which `.result()` re-raises here → propagates
out through `call_sync`). `run_step`: mirror the SDK's decorator (`__init__.py:734-764`)
— bare `@ctx.run_step` uses the function name, else a custom name — resolving through
`self.step`. `headers` property proxies `self.absurd_ctx.headers`. In
`build_task_context`, the non-coroutine branch now returns
`DurableContext(task_result=..., absurd_ctx=ctx, loop=asyncio.get_running_loop())`.
Re-export `DurableContext` from `__init__.py`. (Pass a generous timeout on the
`run_coroutine_threadsafe(...).result(timeout=...)` calls so a mid-op worker shutdown
can't hang the thread.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_durable.py -v` Expected: PASS (sync + async).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/context.py django_absurd/worker.py django_absurd/__init__.py tests/tasks.py tests/core/test_durable.py
git commit -m "feat: sync durable ctx via loop bridge (step/sleep/run_step/heartbeat)"
```

---

### Task 4: Admin — Checkpoints inline + `available_at` on the Runs inline

**Files:**

- Modify: `django_absurd/admin_views.py` (`build_model_field`, checkpoints
  `search_fields`), `django_absurd/admin.py` (`RUN_INLINE_FIELDS`, new checkpoint
  inline, tasks admin wiring)
- Test: `tests/core/test_admin/test_task.py`

**Interfaces:**

- Consumes: the async durable task `asleep_once` (Task 2) to produce a checkpoint row +
  a sleeping run.
- Produces: a `Checkpoint` admin model with a `task` FK relation (attname `task_id`);
  `build_checkpoint_inline(checkpoint_model)`; the tasks admin inlines both runs and
  checkpoints.

- [ ] **Step 1: Write the failing test**

Add to `tests/core/test_admin/test_task.py`:

```python
def test_detail_inlines_checkpoints_and_run_available_at(
    client: Client, admin_user: User
) -> None:
    # asleep_once: a "bump" step (checkpoint) then a sleep -> a sleeping run whose
    # available_at is set. Run one burst to suspend it, then inspect the task page.
    from tests.atasks import DURABLE_STEP_CALLS, asleep_once

    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    asleep_once.enqueue("admin-k")
    call_command("absurd_worker", queue="default", burst=True)  # suspends
    client.force_login(admin_user)

    task = find_task("default", "tests.atasks.asleep_once")
    assert task is not None
    soup = parse_html(client.get(change_url(task.natural_key)))

    groups = soup.select(".inline-group")
    checkpoint_link = soup.select_one('a[href*="/django_absurd/checkpoint/"]')
    assert checkpoint_link is not None  # checkpoints inline drills into the detail
    assert soup.select_one(".field-checkpoint_name") is not None
    available = soup.select_one(".field-available_at")
    assert available is not None
    assert available.get_text(strip=True) != ""  # sleeping run has a wake time
    assert len(groups) >= 2  # runs + checkpoints
```

- [ ] **Step 2: Run test to verify it fails**

Run:
`uv run pytest tests/core/test_admin/test_task.py::test_detail_inlines_checkpoints_and_run_available_at -v`
Expected: FAIL — the task detail inlines runs only; no `.field-checkpoint_name`, no
`.field-available_at` in the run inline.

- [ ] **Step 3: Implement (prose — minimal)**

In `admin_views.build_model_field`, add a branch for
`spec.name == "checkpoints" and col_name == "task_id"` that returns a `task` FK exactly
like the runs branch (`to_field="task_id"`, `db_column="task_id"`,
`db_constraint=False`, `on_delete=DO_NOTHING`, `null=True`,
`related_name="checkpoints"`), and change the checkpoints `EntitySpec.search_fields`
`"task_id"` → `"task__task_id"`. In `admin.py`: add `"available_at"` to
`RUN_INLINE_FIELDS`; add `CHECKPOINT_INLINE_FIELDS` (`checkpoint_name`, `status`,
`state`, `updated_at`); add a `ReadOnlyCheckpointInline` (mirror `ReadOnlyRunInline` —
`fk_name="task"`, read-only perms, `show_change_link`) +
`build_checkpoint_inline(checkpoint_model)`; in `build_entity_admin`'s `tasks` branch,
build the checkpoint model and append its inline to `extra["inlines"]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_admin/test_task.py -v` Expected: PASS (new test +
existing task-admin tests still green — the run inline still shows
`.field-attempt`/`.field-state`).

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

**Interfaces:**

- Consumes: the shipped `DurableContext`/`AsyncDurableContext` API.

- [ ] **Step 1: Write AGENTS.md "Durable steps & sleep" section**

Both variants (sync no-`await`, async `await`); a `step` + `sleep_for` example; and the
**full footgun set** as an explicit list: (a) **effectively-once** — steps persist after
`fn` returns on a separate connection, executed at-least-once in a crash window, keep
side effects idempotent; (b) **deterministic naming/order** — step call order/names must
be stable across replays; `step` and `sleep` share one checkpoint namespace/counter; (c)
**JSON-serializable step returns** — persisted via `json.dumps`; `tuple` → `list` on
replay; (d) **never swallow `SuspendTask`** — re-raise it (and `CancelledTask`); (e)
**long steps** — finish within `claim_timeout` (default 120s) or call `ctx.heartbeat()`;
(f) **absurd-only** — `ctx.step` tasks fail under other Django backends; (g) sleep
resume re-claims the **same** run — attempt does not increment.

- [ ] **Step 2: Write the docs-site page + nav**

Create `docs/web/durable-workflows.md` mirroring the AGENTS.md section (may add
examples/links; must not contradict it). Add a `{"Durable workflows" = ...}` entry to
the `nav` in `zensical.toml`. Add a one-line link in `README.md` (no growth).

- [ ] **Step 3: Extend the web example with a wait-task**

In `examples/web/app.py` add a durable task (a `step` + `sleep_for` ~5s) and a
view/button that enqueues it — demonstrating durable suspend/resume in the browser.

- [ ] **Step 4: Verify docs build + example runs**

Run: `uvx zensical build` Expected: `No issues found`. Then from `examples/web`:
`docker compose up -d --build`, confirm the worker logs the durable task suspending then
completing (bounded ≤30s per the examples convention), then `docker compose down -v`.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/AGENTS.md README.md docs/web/durable-workflows.md zensical.toml examples/web/app.py
git commit -m "docs: durable steps + sleep guide, site page, and web example"
```

---

## Self-Review

**Spec coverage:** scope (step/sleep/heartbeat/headers both + run_step sync-only) →
T1–T3; effectively-once + full footgun docs → T5; conditional-base + frozen/slots
subclass → T1/T3; trio logging fix → T2; admin Checkpoints inline + `available_at` → T4;
pinned test timing recipe + no-monkeypatch → T2/T3; example → T5. All spec sections
mapped.

**Type consistency:** `AsyncDurableContext`/`DurableContext` field names (`absurd_ctx`,
`loop`) and method signatures (`step(name, fn)`, `sleep_for(name, seconds)`,
`sleep_until(name, when)`, `heartbeat(seconds=None)`, `headers` property, sync-only
`run_step`) are consistent across T1/T2/T3 and match the spec. `build_task_context`'s
variant choice (T1 async, T3 sync) is additive — the sync branch stays plain
`TaskContext` between T1 and T3, so no task is half-wired.

**Placeholders:** none — every step is a concrete test or a concrete prose change.
