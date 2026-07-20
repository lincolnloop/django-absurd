# Events (await_event / emit_event) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ship Absurd's Events pillar in django-absurd — `await_event`/`emit_event`
in-task, plus a top-level `django_absurd.emit_event` for use from outside a task (a
view/webhook). Closes #21.

**Architecture:** thin delegation to the already-shipped context-accessor pattern (#84).
Async is free (SDK's `AsyncTaskContext` already has `await_event`/`emit_event`); sync
gets two new bridge methods on `AbsurdTaskContext` mirroring `sleep_for`/`heartbeat`.
The top-level `emit_event` is a new `django_absurd/events.py` module, lazy-imported from
`__init__.py` to dodge `AppRegistryNotReady`. A Waits admin inline mirrors the shipped
Checkpoints inline verbatim.

**Tech Stack:** Django 6.0 / Python 3.12+, `absurd_sdk`, psycopg3, pytest
(function-based).

## Global Constraints

- Floor: Django 6.0 / Python 3.12. psycopg (v3) backend only.
- `import typing as t` (never `from typing import X`); absolute imports only.
- Functions verb-named; no leading-underscore module constants/helpers.
- No monkeypatching / `unittest.mock.patch`; behavioral tests through real entrypoints
  (task + worker drain, admin HTTP, management commands) — never raw unit calls into
  helpers, never raw-SQL assertions for a happy path we can reach through code.
- HTTP mocking (if ever needed): `responses`, not `mock`. (Not needed in this plan.)
- No ruff ignores added without asking first — hit one here (`A004`, see Task 1) and
  resolved it by NOT re-exporting `TimeoutError`; import it directly from `absurd_sdk`,
  `import absurd_sdk` then `absurd_sdk.TimeoutError` (never
  `from absurd_sdk import TimeoutError` — same A004 hit in any file).
- Full patch coverage: every added line/branch covered via a real entrypoint.
- Assert complete error-message text (not fragments) in new tests.
- Alphabetize `@pytest.mark.parametrize` values / fixture params (n/a here — no new
  parametrized tests).
- `AbsurdBackend`/project is a hard singleton (#63/E004) — `get_absurd_backend()` /
  `get_declared_queues()` already assume this; do not build multi-backend plumbing.
- `await_task_result` is OUT OF SCOPE (deliberately not built) — do not add it.
- Docs mirror between `django_absurd/AGENTS.md` and `docs/web/workflows.md`; build clean
  with `uvx zensical build` after doc edits.
- Commit after each task (local branch `events-await-emit`, no push/PR without asking —
  per project convention, new-feature work stays local + reviewed before any PR).

---

### Task 1: Top-level `django_absurd.emit_event` (outside-a-task signal)

**Files:**

- Create: `django_absurd/events.py`
- Modify: `django_absurd/__init__.py`
- Test: `tests/core/test_events.py` (new)
- Test: `tests/core/test_admin/test_event.py` (add one case)

**Interfaces:**

- Produces:
  `django_absurd.events.emit_event(event_name: str, payload: "JsonValue | None" = None, *, queue: str = "default") -> None`,
  re-exported as `django_absurd.emit_event`. Raises
  `django.core.exceptions.ImproperlyConfigured` on: no `AbsurdBackend` configured;
  `queue` not in `get_declared_queues(backend)`; the queue's table not yet provisioned
  (`psycopg.errors.UndefinedTable`).
- Consumes: `django_absurd.queues.get_absurd_backend()`,
  `django_absurd.queues.get_absurd_client()`,
  `django_absurd.backends.get_declared_queues(backend)` (all existing, imported
  **inside** the function body — a module-level import of
  `django_absurd.queues`/`django_absurd.backends` here would transitively pull in
  `django_absurd.models` before the app registry is ready, since `__init__.py` imports
  this module at top level).

- [ ] **Step 1: Write the failing tests in `tests/core/test_events.py`**

```python
import pytest
from django.core.exceptions import ImproperlyConfigured
from pytest_django.fixtures import SettingsWrapper

from django_absurd import emit_event

pytestmark = pytest.mark.django_db(transaction=True)


def test_top_level_emit_event_unknown_queue_raises() -> None:
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"Queue 'ghost' is not declared in TASKS QUEUES\. Add it to the QUEUES "
            r"list in your TASKS backend settings\."
        ),
    ):
        emit_event("whatever", queue="ghost")


def test_top_level_emit_event_no_backend_configured_raises(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    with pytest.raises(
        ImproperlyConfigured, match=r"django-absurd: no Absurd backend configured\."
    ):
        emit_event("whatever")


def test_top_level_emit_event_unsynced_queue_raises(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}, "unsynced": {}}},
        }
    }
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"Queue 'unsynced' is declared but its Absurd table is not provisioned\. "
            r"Run: manage\.py absurd_sync_queues"
        ),
    ):
        emit_event("whatever", queue="unsynced")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_events.py -v` Expected: FAIL —
`ImportError: cannot import name 'emit_event' from 'django_absurd'`

- [ ] **Step 3: Write `django_absurd/events.py`**

```python
"""Emit an Absurd event from outside a running task (e.g. a Django view)."""

import typing as t

import psycopg.errors
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

if t.TYPE_CHECKING:
    from absurd_sdk import JsonValue


def emit_event(
    event_name: str, payload: "JsonValue | None" = None, *, queue: str = "default"
) -> None:
    from django_absurd.backends import get_declared_queues
    from django_absurd.queues import get_absurd_backend, get_absurd_client

    backend = get_absurd_backend()
    if backend is None:
        msg = "django-absurd: no Absurd backend configured."
        raise ImproperlyConfigured(msg)
    declared = get_declared_queues(backend)
    if queue not in declared:
        msg = (
            f"Queue '{queue}' is not declared in TASKS QUEUES. "
            "Add it to the QUEUES list in your TASKS backend settings."
        )
        raise ImproperlyConfigured(msg)
    client = get_absurd_client()
    try:
        with transaction.atomic(using=backend.database, savepoint=True):
            client.emit_event(event_name, payload, queue_name=queue)
    except psycopg.errors.UndefinedTable:
        msg = (
            f"Queue '{queue}' is declared but its Absurd table is not provisioned. "
            "Run: manage.py absurd_sync_queues"
        )
        raise ImproperlyConfigured(msg) from None
```

- [ ] **Step 4: Re-export from `django_absurd/__init__.py`**

Modify the existing import block and `__all__`:

```python
from django_absurd.context import (
    AbsurdTaskContext,
    aget_absurd_context,
    get_absurd_context,
)
from django_absurd.events import emit_event

ABSURD_SCHEMA_VERSION = "0.4.0"

__all__ = [
    "ABSURD_SCHEMA_VERSION",
    "AbsurdTaskContext",
    "aget_absurd_context",
    "emit_event",
    "get_absurd_context",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_events.py -v` Expected: PASS (3 tests)

- [ ] **Step 6: Add the happy-path admin-visible test**

`tests/core/test_admin/test_event.py` already seeds the `events` queue table with raw
SQL to test the generic changelist/detail plumbing (from #84). Add a case that seeds via
the real `emit_event()` entrypoint instead, so the write path itself is exercised
through the admin's read path:

```python
from django_absurd import emit_event

...


def test_emit_event_writes_a_visible_row(client: Client, admin_user: t.Any) -> None:
    call_command("absurd_sync_queues")
    emit_event("order.shipped:demo", {"id": 1}, queue="default")
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    names = set()
    for r in result_rows(soup):
        elem = r.select_one(".field-event_name")
        assert elem is not None
        names.add(elem.get_text(strip=True))
    assert "order.shipped:demo" in names
```

(Add the `from django_absurd import emit_event` import alongside the existing imports at
the top of the file.)

- [ ] **Step 7: Run the full test_event.py + test_events.py, verify pass**

Run: `uv run pytest tests/core/test_events.py tests/core/test_admin/test_event.py -v`
Expected: PASS (3 + 2 tests)

- [ ] **Step 8: mypy + ruff clean**

Run: `uv run mypy django_absurd/events.py django_absurd/__init__.py` Run:
`uv run ruff check django_absurd/events.py django_absurd/__init__.py tests/core/test_events.py tests/core/test_admin/test_event.py`
Expected: no errors (confirm `A004` does NOT fire — `events.py` never imports the name
`TimeoutError`, so this task doesn't trip it; that hazard is Task 2's).

- [ ] **Step 9: Commit**

```bash
git add django_absurd/events.py django_absurd/__init__.py tests/core/test_events.py tests/core/test_admin/test_event.py
git commit -m "feat: top-level django_absurd.emit_event for outside-a-task signals"
```

---

### Task 2: Sync bridge — `AbsurdTaskContext.await_event` / `.emit_event`

**Files:**

- Modify: `django_absurd/context.py`
- Modify: `tests/tasks.py` (sync test tasks)
- Modify: `tests/atasks.py` (async test tasks)
- Modify: `tests/core/test_events.py` (behavioral suite)

**Interfaces:**

- Produces:
  `AbsurdTaskContext.await_event(event_name: str, step_name: str | None = None, timeout: int | None = None) -> JsonValue`;
  `AbsurdTaskContext.emit_event(event_name: str, payload: JsonValue | None = None) -> None`.
  Both bridge `self.absurd_ctx.{await_event,emit_event}` over `run_on_loop`, exactly
  like `sleep_for`/`heartbeat`.
- Consumes (from Task 1):
  `django_absurd.emit_event(event_name, payload=None, *, queue="default")` — used by the
  tests below to wake a suspended waiter from outside the task.
- Consumes: `absurd_sdk.TimeoutError` — imported as `import absurd_sdk` then referenced
  as `absurd_sdk.TimeoutError` (never `from absurd_sdk import TimeoutError` — that trips
  ruff's `A004` builtin-shadowing in ANY file, confirmed by running ruff; this is why
  django-absurd does not re-export it either, see Global Constraints).

- [ ] **Step 1: Add sync test tasks to `tests/tasks.py`**

Add near the bottom, after `ssleep_until_once`:

```python
import absurd_sdk  # add to the top-of-file import block
```

```python
@task
def sawait_event_once(name: str) -> t.Any:
    return get_absurd_context().await_event(name)


@task
def semit_event_once(name: str, payload: t.Any) -> None:
    get_absurd_context().emit_event(name, payload)


@task
def sawait_event_timeout(name: str) -> str:
    try:
        get_absurd_context().await_event(name, timeout=0)
    except absurd_sdk.TimeoutError:
        return "timed-out"
    return "no-timeout"
```

- [ ] **Step 2: Add async test tasks to `tests/atasks.py`**

Add near the bottom, after `asleep_until_once`:

```python
@task
async def aawait_event_once(name: str) -> t.Any:
    return await aget_absurd_context().await_event(name)


@task
async def aemit_event_once(name: str, payload: t.Any) -> None:
    await aget_absurd_context().emit_event(name, payload)
```

- [ ] **Step 3: Write the failing tests in `tests/core/test_events.py`**

Add these functions (this file already has the Task 1 tests from above):

```python
from django.core.management import call_command

from tests import atasks, tasks, utils


def test_sync_await_event_suspends_then_top_level_emit_resumes() -> None:
    call_command("absurd_sync_queues")
    result = tasks.sawait_event_once.enqueue("order.packed:sync-1")

    utils.run_absurd_worker()  # drain 1: no event yet -> suspend
    suspended = utils.get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    emit_event("order.packed:sync-1", {"tracking": "abc"}, queue="default")

    utils.run_absurd_worker()  # drain 2: resumes with the payload
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == {"tracking": "abc"}


def test_async_await_event_suspends_then_top_level_emit_resumes() -> None:
    call_command("absurd_sync_queues")
    result = atasks.aawait_event_once.enqueue("order.packed:async-1")

    utils.run_absurd_worker()
    suspended = utils.get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    emit_event("order.packed:async-1", {"tracking": "abc"}, queue="default")

    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == {"tracking": "abc"}


def test_emit_before_await_returns_immediately_no_suspend() -> None:
    call_command("absurd_sync_queues")
    emit_event("order.packed:before-1", {"tracking": "xyz"}, queue="default")

    result = tasks.sawait_event_once.enqueue("order.packed:before-1")
    utils.run_absurd_worker()  # single drain: event already there, no suspend
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == {"tracking": "xyz"}


def test_first_emit_per_name_wins() -> None:
    call_command("absurd_sync_queues")
    emit_event("order.packed:first-wins", {"tracking": "first"}, queue="default")
    emit_event("order.packed:first-wins", {"tracking": "second"}, queue="default")

    result = tasks.sawait_event_once.enqueue("order.packed:first-wins")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.result == {"tracking": "first"}


def test_in_task_emit_event_wakes_a_separately_enqueued_waiter() -> None:
    call_command("absurd_sync_queues")
    tasks.semit_event_once.enqueue("order.packed:in-task", {"tracking": "in-task"})
    utils.run_absurd_worker()

    result = tasks.sawait_event_once.enqueue("order.packed:in-task")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.result == {"tracking": "in-task"}


def test_async_in_task_emit_event_wakes_a_separately_enqueued_waiter() -> None:
    call_command("absurd_sync_queues")
    atasks.aemit_event_once.enqueue("order.packed:in-task-async", {"tracking": "async"})
    utils.run_absurd_worker()

    result = atasks.aawait_event_once.enqueue("order.packed:in-task-async")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.result == {"tracking": "async"}


def test_uncaught_timeout_raises_absurd_sdk_timeout_error_and_is_catchable() -> None:
    call_command("absurd_sync_queues")
    result = tasks.sawait_event_timeout.enqueue("order.packed:never-arrives")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == "timed-out"
```

(`emit_event` is already imported at the top of this file from Task 1.)

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_events.py -v` Expected: FAIL —
`AttributeError: 'AbsurdTaskContext' object has no attribute 'await_event'`

- [ ] **Step 5: Add the bridge methods to `django_absurd/context.py`**

Extend the `TYPE_CHECKING` import block:

```python
if t.TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Callable, Coroutine, Mapping

    from absurd_sdk import JsonValue
```

Add the two methods to `AbsurdTaskContext`, after `sleep_until`:

```python
    def await_event(
        self, event_name: str, step_name: str | None = None, timeout: int | None = None
    ) -> "JsonValue":
        return self.run_on_loop(
            self.absurd_ctx.await_event(event_name, step_name, timeout)
        )

    def emit_event(self, event_name: str, payload: "JsonValue | None" = None) -> None:
        self.run_on_loop(self.absurd_ctx.emit_event(event_name, payload))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_events.py -v` Expected: PASS (all 10 tests — 3 from
Task 1, 7 from this task)

- [ ] **Step 7: mypy + ruff clean**

Run: `uv run mypy django_absurd/context.py tests/tasks.py tests/atasks.py` Run:
`uv run ruff check django_absurd/context.py tests/tasks.py tests/atasks.py tests/core/test_events.py`
Expected: no errors — in particular confirm `tests/tasks.py`'s
`except absurd_sdk.TimeoutError` does NOT trip `A004` (it references the attribute, not
a bare imported name).

- [ ] **Step 8: Full core suite green**

Run: `uv run pytest tests/core -v` Expected: PASS, no regressions.

- [ ] **Step 9: Commit**

```bash
git add django_absurd/context.py tests/tasks.py tests/atasks.py tests/core/test_events.py
git commit -m "feat: AbsurdTaskContext.await_event/.emit_event sync bridge"
```

---

### Task 3: Waits admin inline (mirrors the shipped Checkpoints inline)

**Files:**

- Modify: `django_absurd/admin_views.py`
- Modify: `django_absurd/admin.py`
- Modify: `tests/core/test_admin/test_task.py`

**Interfaces:**

- Consumes (from Task 2): `tests.tasks.sawait_event_once` — used to produce a real
  suspended `Wait` row through the worker (no raw SQL seeding for this inline test, same
  as the existing Checkpoints-inline test).
- Produces:
  `django_absurd.admin.build_wait_inline(wait_model) -> type[admin.TabularInline]`,
  wired into the tasks admin's `inlines` alongside Runs + Checkpoints.

- [ ] **Step 1: Write the failing test in `tests/core/test_admin/test_task.py`**

Add `sawait_event_once` to the existing `from tests.tasks import add` line:

```python
from tests.tasks import add, sawait_event_once
```

Add this test, alongside `test_detail_inlines_checkpoints_and_run_available_at`:

```python
def test_detail_inlines_waits_for_a_suspended_await_event(
    client: Client, admin_user: User
) -> None:
    call_command("absurd_sync_queues")
    sawait_event_once.enqueue("wait-admin-demo")
    call_command("absurd_worker", queue="default", burst=True)  # suspends
    client.force_login(admin_user)

    task = find_task("default", "tests.tasks.sawait_event_once")
    assert task is not None
    soup = parse_html(client.get(change_url(task.natural_key)))

    assert soup.select_one('a[href*="/django_absurd/wait/"]') is not None
    names = {cell.get_text(strip=True) for cell in soup.select(".field-event_name")}
    assert "wait-admin-demo" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run:
`uv run pytest tests/core/test_admin/test_task.py::test_detail_inlines_waits_for_a_suspended_await_event -v`
Expected: FAIL — no `a[href*="/django_absurd/wait/"]` in the page (no Waits inline yet).

- [ ] **Step 3: Add the FK field branch in `django_absurd/admin_views.py`**

In `build_model_field`, add this branch right after the existing `checkpoints`/`task_id`
branch (before the final `return col_name, make_field(col_type)`):

```python
    # Waits join to their task on task_id — same constraint-free FK treatment as runs
    # and checkpoints so the admin can inline waits under a task. The attname stays
    # task_id.
    if spec.name == "waits" and col_name == "task_id":
        tasks_spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
        return "task", models.ForeignKey(
            build_admin_model(tasks_spec),
            to_field="task_id",
            db_column="task_id",
            db_constraint=False,
            on_delete=models.DO_NOTHING,
            null=True,
            related_name="waits",
        )
```

Update the `waits` `EntitySpec`'s `search_fields` (first element only):

```python
        search_fields=("task__task_id", "run_id", "step_name"),
```

- [ ] **Step 4: Add the inline classes in `django_absurd/admin.py`**

Add `WAIT_INLINE_FIELDS` after `CHECKPOINT_INLINE_FIELDS`:

```python
WAIT_INLINE_FIELDS = (
    "event_name",
    "step_name",
    "timeout_at",
    "created_at",
)
```

Add `ReadOnlyWaitInline` + `build_wait_inline` after `build_checkpoint_inline`:

```python
class ReadOnlyWaitInline(_TabularInlineBase):
    fk_name = "task"
    extra = 0
    can_delete = False
    show_change_link = True
    ordering = ("created_at",)
    fields = WAIT_INLINE_FIELDS
    readonly_fields = WAIT_INLINE_FIELDS

    def has_add_permission(
        self, request: "HttpRequest", obj: "models.Model | None" = None
    ) -> bool:
        return False

    def has_change_permission(
        self, request: "HttpRequest", obj: "models.Model | None" = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: "HttpRequest", obj: "models.Model | None" = None
    ) -> bool:
        return False

    def has_view_permission(
        self, request: "HttpRequest", obj: "models.Model | None" = None
    ) -> bool:
        return True


def build_wait_inline(
    wait_model: "type[Model]",
) -> "type[admin.TabularInline[t.Any, t.Any]]":
    return type("WaitInline", (ReadOnlyWaitInline,), {"model": wait_model})
```

Wire it into `build_entity_admin`'s `spec.name == "tasks"` branch:

```python
    if spec.name == "tasks":
        run_model = build_admin_model(
            next(s for s in ADMIN_ENTITY_SPECS if s.name == "runs")
        )
        checkpoint_model = build_admin_model(
            next(s for s in ADMIN_ENTITY_SPECS if s.name == "checkpoints")
        )
        wait_model = build_admin_model(
            next(s for s in ADMIN_ENTITY_SPECS if s.name == "waits")
        )
        extra["inlines"] = [
            build_run_inline(run_model),
            build_checkpoint_inline(checkpoint_model),
            build_wait_inline(wait_model),
        ]
        extra["fieldsets"] = TASK_FIELDSETS
        extra["ordering"] = ("-first_started_at", "-enqueue_at")
```

- [ ] **Step 5: Run test to verify it passes**

Run:
`uv run pytest tests/core/test_admin/test_task.py::test_detail_inlines_waits_for_a_suspended_await_event -v`
Expected: PASS

- [ ] **Step 6: Run the full admin suite + mypy**

Run: `uv run pytest tests/core/test_admin -v` Run:
`uv run mypy django_absurd/admin.py django_absurd/admin_views.py` Expected: all PASS, no
regressions in `test_wait.py`'s existing composite-detail test (the `search_fields`
change is additive — `task_id` → `task__task_id` only changes what the search box
matches, not the changelist/detail rendering `test_wait.py` checks).

- [ ] **Step 7: Commit**

```bash
git add django_absurd/admin_views.py django_absurd/admin.py tests/core/test_admin/test_task.py
git commit -m "feat: Waits admin inline under the task detail page"
```

---

### Task 4: Docs — Events section (AGENTS.md + docs/web/workflows.md)

**Files:**

- Modify: `django_absurd/AGENTS.md`
- Modify: `docs/web/workflows.md`

**Interfaces:**

- Consumes (from Tasks 1–2): `get_absurd_context().await_event/.emit_event`,
  `aget_absurd_context().await_event/.emit_event` (SDK passthrough), top-level
  `django_absurd.emit_event`, `absurd_sdk.TimeoutError`.
- No code interfaces produced — docs only. No test step (docs aren't pytest-covered);
  verification is the `zensical build` in Step 3.

- [ ] **Step 1: Extend `docs/web/workflows.md`**

Update the intro paragraph (top of file) to mention Events:

```markdown
Absurd calls these primitives **Steps (Checkpoints)**, **Sleep**, and **Events** — see
[Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/). They let a task
break its work into checkpointed steps, sleep between them, and suspend until a named
signal arrives — persisting progress so retries and resumes pick up where they left off,
never redoing completed steps. This page covers the django-absurd surface: the
`get_absurd_context()` / `aget_absurd_context()` accessors.
```

Insert a new `## Events` section right after `## Sleep` (before `## API`):

````markdown
## Events

`context.await_event(event_name, step_name=None, timeout=None)` suspends the task until
a named event arrives, then returns its JSON payload.
`context.emit_event(event_name, payload=None)` emits an event on the task's own queue
(in-task, replay-safe — a re-emit after a retry is a no-op). Events are awaited by name,
carry an optional JSON payload, and **first emit per name wins** (immutable) — a
business-keyed name like `"warehouse.packed:order-42"` targets exactly one waiter.

→ [Absurd: Concepts — Events](https://earendil-works.github.io/absurd/concepts/#events)

Events are **queue-scoped**: `await_event`/`emit_event` operate on the task's own queue.
An event emitted on queue X only wakes a waiter on queue X.

### The outside-a-task signal: top-level `emit_event`

`ctx.emit_event` only reaches code running _inside_ a task. The real-world signal that
wakes a waiter — a webhook, a view, an API handler — is ordinary Django code, not a
task. `django_absurd.emit_event(event_name, payload=None, *, queue="default")` is that
entry point:

```python
from django_absurd import emit_event


def warehouse_webhook(request, order):
    emit_event(f"warehouse.packed:{order}", {"tracking": request.POST["tracking"]},
               queue="default")
    return HttpResponse(status=204)
```

End-to-end: a task calls `await_event(f"warehouse.packed:{order}")` → suspends (worker
freed) → the warehouse system POSTs the webhook → the view emits the event on the task's
queue → the task's next claim finds it → resumes with the payload.

`queue` must match the queue the waiting task actually runs on — it targets the
client-level `emit_event`'s `queue_name`, not a database alias. An unknown queue raises
`ImproperlyConfigured` immediately (fail fast on a typo). `emit_event` is sync; from an
async view, wrap it in `sync_to_async`.

### Sync

```python
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    payload = context.await_event(f"warehouse.packed:{order_id}")
    context.step("ship", lambda: ship(order_id, payload))
```

### Async

```python
from django.tasks import task
from django_absurd import aget_absurd_context


@task
async def process_order(order_id: int) -> None:
    context = aget_absurd_context()
    payload = await context.await_event(f"warehouse.packed:{order_id}")

    async def ship_order():
        return await ship(order_id, payload)

    await context.step("ship", ship_order)
```

### Timeout

Pass `timeout` (seconds) to stop waiting after a bound. On timeout, `await_event` raises
`absurd_sdk.TimeoutError` — **not** the builtin `TimeoutError`:

```python
import absurd_sdk
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> str:
    context = get_absurd_context()
    try:
        context.await_event(f"warehouse.packed:{order_id}", timeout=3600)
    except absurd_sdk.TimeoutError:
        return "gave up waiting for the warehouse"
    return "shipped"
```

!!! warning "Not the builtin `TimeoutError`"

    `except TimeoutError:` (the builtin) does **not** catch this — you must
    `import absurd_sdk` and catch `absurd_sdk.TimeoutError` explicitly.

An **uncaught** `TimeoutError` fails the run, which then retries and re-waits the full
`timeout` on each attempt until `max_attempts` — catch it if you want a one-shot
timeout.

### `await_task_result` is not provided

Absurd's SDK version of this polls + heartbeats inside a step rather than suspending
(holding the worker slot), and is cross-queue-only. For a child task's result, use
Django's `get_result()` / `aget_result()` instead.
````

Extend the `## API` table with two new rows:

```markdown
| `await_event(event_name, step_name=None, timeout=None)` | yes | `await` | Suspend
until the named event arrives; return its payload | |
`emit_event(event_name, payload=None)` | yes | `await` | Emit an event on the task's own
queue (replay-safe) |
```

Extend `## Caveats` with two new subsections, after `### Absurd backend only`:

```markdown
### Events are subject to cleanup_ttl

An event emitted long before a delayed `await_event` can be cleaned up by the queue's
`cleanup_ttl` before the waiter ever checks — the waiter then never wakes. Keep
`cleanup_ttl` generous relative to how long a waiter might sleep before checking.

### `TimeoutError` is `absurd_sdk.TimeoutError`, not the builtin

`except TimeoutError:` silently catches nothing — `import absurd_sdk` and catch
`absurd_sdk.TimeoutError`.
```

Extend `## Admin introspection`:

```markdown
Waits are visible in Django admin under **Waits** (one row per task suspended in
`await_event`), and inline under the task detail page alongside Runs and Checkpoints.
```

- [ ] **Step 2: Mirror the same content into `django_absurd/AGENTS.md`**

`AGENTS.md`'s Workflows section (`## Workflows` → `### Steps (checkpoints)` →
`### Sleep` → `### API reference` → `### Caveats`) mirrors `docs/web/workflows.md`
almost verbatim but with `###` headings instead of `##`/`###`. Apply the same content as
Step 1 (intro line, Events section, API table rows, Caveats additions), demoting each
heading by one level (`## Events` → `### Events`, `### Sync`/`### Async`/`### Timeout` →
`#### Sync`/`#### Async`/`#### Timeout`, etc.) to match the existing nesting in this
file, inserted between the existing `### Sleep` and `### API reference` sections.

- [ ] **Step 3: Build docs clean**

Run: `uvx zensical build` Expected: build succeeds, no broken links/anchors.

- [ ] **Step 4: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/workflows.md
git commit -m "docs: Events section (await_event/emit_event) on Workflows page"
```

---

### Task 5: Example — order-fulfillment uses real `await_event`

**Files:**

- Modify: `examples/web/app.py`
- Modify: `examples/README.md`

**Interfaces:**

- Consumes (from Tasks 1–2): `context.await_event(event_name)`,
  `django_absurd.emit_event(event_name, payload, queue="default")`.
- No test step — this is a nanodjango demo app, run manually to confirm it works (see
  Step 4).

- [ ] **Step 1: Replace the `sleep_for` stand-in with real `await_event` in
      `examples/web/app.py`**

Update the import lines — add the `emit_event` import and a `url_quote` alias for
`urllib.parse.quote` (the file already imports `quote` from `django.contrib.admin.utils`
for admin pk-quoting, so this needs a distinct name):

```python
from urllib.parse import quote as url_quote

from django_absurd import emit_event, get_absurd_context
```

Replace `fulfill_order`:

```python
@task
def fulfill_order(order: str) -> str:
    """Order-fulfillment workflow: charge, reserve inventory, wait, notify.

    Mirrors the shape of Absurd's headline order-fulfillment example
    (https://github.com/earendil-works/absurd#readme): step(charge) →
    step(reserve inventory) → await_event(warehouse packed) → step(notify).

    Each step is a checkpoint: check the admin's Checkpoints, Waits, and Runs
    pages to see the steps and the suspended state while it waits.

    Shows both step forms: ``context.step(name, fn)`` and the ``run_step``
    decorator (sync only), which runs the function once and replaces it with the
    step's return value.
    """
    context = get_absurd_context()
    context.step("charge", lambda: f"charged: {order}")
    context.step("reserve-inventory", lambda: f"reserved: {order}")
    context.await_event(f"warehouse.packed:{order}")

    @context.run_step("notify")
    def notify() -> str:
        return f"notified: {order}"

    return notify
```

- [ ] **Step 2: Route the workflow result to a page showing a "mark packed" button**

Update `workflow_view`'s success redirect:

```python
        if form.is_valid():
            order = form.cleaned_data["order"]
            result = fulfill_order.enqueue(order=order)
            return redirect(f"/tasks/{result.id}/?order={order}")
```

Update `workflow_view`'s copy paragraph:

```python
        <p>
          Mirrors Absurd's
          <a href="https://github.com/earendil-works/absurd#readme">order-fulfillment
          example</a>: <em>charge</em>, <em>reserve-inventory</em>,
          <code>await_event</code> for the warehouse to pack the order, <em>notify</em>.
          While waiting, check
          <a href="/admin/django_absurd/run/">Runs</a>,
          <a href="/admin/django_absurd/checkpoint/">Checkpoints</a>, and
          <a href="/admin/django_absurd/wait/">Waits</a> in the admin — or click
          "mark packed" below once the task detail page appears.
        </p>
```

Add a new route, near `workflow_view`:

```python
@app.route("/workflow/<str:order>/pack/")
def pack_view(request: HttpRequest, order: str) -> HttpResponse:
    if request.method == "POST":
        emit_event(f"warehouse.packed:{order}", {"packed_by": "warehouse demo"},
                   queue="default")
    next_url = request.GET.get("next", "/")
    return redirect(next_url)
```

Update `task_detail` to show the button when an `order` is present and the task isn't
finished yet:

```python
@app.route("/tasks/<str:result_id>/")
def task_detail(request: HttpRequest, result_id: str) -> HttpResponse | str:
    try:
        result = default_task_backend.get_result(result_id)
    except TaskResultDoesNotExist:
        return HttpResponse(f"<h1>Unknown task {result_id}</h1>", status=404)

    finished = result.status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED)
    refresh = "" if finished else '<meta http-equiv="refresh" content="1">'
    if result.status == TaskResultStatus.SUCCESSFUL:
        body = f"<p>Result: <strong>{result.return_value}</strong></p>"
    elif result.status == TaskResultStatus.FAILED:
        body = f"<p>Failed: {result.errors}</p>"
    else:
        body = "<p>Working… (auto-refreshing)</p>"

    order = request.GET.get("order")
    pack_button = ""
    if order and not finished:
        back = url_quote(f"/tasks/{result_id}/?order={order}", safe="")
        pack_button = f"""
        <form method="post" action="/workflow/{order}/pack/?next={back}">
          <input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">
          <button type="submit">Mark "{order}" packed by the warehouse</button>
        </form>
        """

    fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
    dump = html.escape(pprint.pformat(fields))
    admin_url = reverse("admin:django_absurd_task_change", args=[quote(result.id)])
    return f"""
        {refresh}
        <h1>Task {result.id}</h1>
        <p>Status: <strong>{result.status.name}</strong></p>
        {body}
        {pack_button}
        <pre><code>{dump}</code></pre>
        <p>
          <a href="/">Add another</a> ·
          <a href="{admin_url}">View this task in the admin</a>
          — its Runs + Checkpoints + Waits inlines show the steps and suspended state.
        </p>
    """
```

Update the module docstring at the top of the file:

```python
"""Single-file nanodjango demo: django-absurd enqueue + result.

Enqueue add(a, b) from a form; the worker runs it; watch the result page and
browse the read-only queue tables in the admin (auto-registered by django-absurd).

Also demonstrates Steps (checkpoints) + Sleep + Events: an order-fulfillment
workflow that checkpoints each step and suspends on await_event until a
"mark packed" button emits the matching event.

    docker compose up
    http://localhost:8000/         enqueue add(a, b) or the order workflow
    http://localhost:8000/admin/   Tasks / Runs / Checkpoints / Waits / … (admin / admin)

psycopg (v3) backend required — DATABASES is overridden (nanodjango defaults to sqlite).
"""
```

- [ ] **Step 3: Update `examples/README.md`**

Replace the `web/` bullet's Steps+Sleep description:

```markdown
- **[`web/`](web/)** — enqueue `add(a, b)` from a form and watch the result
  (`get_result`); browse the read-only queue tables in the admin. Also demonstrates
  **Steps (checkpoints), Sleep, and Events** at `/workflow/` — an order-fulfillment task
  that checkpoints each step and suspends on `await_event` until a "mark packed" button
  (calling the top-level `emit_event`) wakes it, with a link into the task's admin page
  to watch its checkpoints and suspended wait.
```

- [ ] **Step 4: Run the example manually to confirm it works**

```bash
cd examples/web
docker compose up
```

Open `http://localhost:8000/`, submit the order-fulfillment form, confirm the task
detail page auto-refreshes, shows "Working…", and shows the "mark packed" button. Click
it, confirm the task completes and its result shows `notified: <order>`. Check
`/admin/django_absurd/wait/` shows the resolved wait, and the task detail's Waits inline
shows the same `event_name`. Stop with `docker compose down`.

- [ ] **Step 5: Commit**

```bash
git add examples/web/app.py examples/README.md
git commit -m "docs(examples): order-fulfillment demo uses real await_event/emit_event"
```

---

## Self-Review Notes

- **Spec coverage:** `await_event`/in-task `emit_event` (Task 2) · top-level
  `emit_event` (Task 1) · `TimeoutError` handling — direct import, not re-exported, per
  the amended spec (Task 2 + docs in Task 4) · Waits admin inline (Task 3) ·
  docs/tests/example (Tasks 1–5) · `await_task_result` cut (Global Constraints, and
  absent throughout).
- **Placeholder scan:** none — every step has real code, exact test names, exact
  commands.
- **Type consistency:** `AbsurdTaskContext.await_event`/`.emit_event` (Task 2) match the
  SDK signatures verbatim, matching what Tasks 1/3/5 call. `django_absurd.emit_event`'s
  signature (Task 1) matches what Task 2's tests and Task 5's example call.
