# Tasks-API Enqueue (SP2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `AbsurdBackend.enqueue` so `my_task.enqueue(...)` writes a real
Absurd task row via `client.spawn(task.module_path, …)`, returning a Django
`TaskResult`.

**Architecture:** Sync `enqueue` only, on the existing `AbsurdBackend` (SP1). It reuses
Django's psycopg connection (via `get_absurd_client`), so the spawn INSERT rides the
current DB transaction (enqueue-on-commit is automatic). `aenqueue` comes free from
`BaseTaskBackend` (sync_to_async wrap). Result retrieval, native async, defer, and
priority are out of scope (flags `False`).

**Tech Stack:** Django 6.0 `django.tasks`, absurd-sdk (`spawn`/`claim_tasks`), psycopg3,
pytest + pytest-django.

## Global Constraints

- Floor: Django 6.0 / Python 3.12 (already in place from SP1).
- Support flags on `AbsurdBackend`: `supports_get_result=False`,
  `supports_async_task=False`, `supports_defer=False`, `supports_priority=False`.
- `enqueue` payload: `{"args": list(args), "kwargs": kwargs}`; `queue=task.queue_name`;
  `max_attempts=self.default_max_attempts`. `spawn` returns
  `{"task_id", "run_id", "attempt"}`. Returned `TaskResult`: `id=spawn["task_id"]`,
  `status=TaskResultStatus.READY`, `args=list(args)`, `kwargs=dict(kwargs)`,
  `backend=self.alias`, `enqueued_at=timezone.now()`, other timestamps `None`,
  `errors=[]`, `worker_ids=[]`.
- Client: `get_absurd_client(self.database)` — sync, reuses Django's connection (asserts
  psycopg3). The spawn shares Django's transaction.
- `aenqueue` is NOT overridden (base provides it).
- Imports: `import typing as t` (never `from typing import X`); absolute imports only.
- Functions contain a verb; no leading-underscore module names; helpers BELOW their
  public callers.
- pytest function-based only; `@pytest.mark.django_db(transaction=True)` for these tests
  (spawn commits + queue DDL). The autouse `_reset_absurd_queues` fixture drops all
  queues per-test, so each test that enqueues runs `call_command("absurd_sync_queues")`
  first. No mocks. Verify spawns by claiming them back
  (`get_absurd_client().claim_tasks(batch_size=1)` → rows with `task_name`, `params`).
- DB: `docker compose up -d db`; run with `PGPORT=5433`.

---

### Task 1: `enqueue` + support flags

**Files:**

- Modify: `django_absurd/backends.py` (implement `enqueue`; set the four `supports_*`
  flags; remove the `enqueue` `NotImplementedError` stub)
- Create: `tests/tasks.py` (real `@task` functions for the tests)
- Create: `tests/test_enqueue.py`

**Interfaces:**

- Consumes: `get_absurd_client(using=None)` and
  `AbsurdBackend.{alias,database,default_max_attempts}` (SP1,
  `django_absurd/queues.py` + `backends.py`).
- Produces:

  - `AbsurdBackend.enqueue(self, task, args, kwargs) -> TaskResult` (implemented).
  - `AbsurdBackend` class attrs `supports_get_result=False`,
    `supports_async_task=False`, `supports_defer=False`, `supports_priority=False`.
  - `tests/tasks.py`: `add(a, b)` (sync `@task`), `add_async(a, b)` (async `@task`).

- [ ] **Step 1: Write the test task module + failing tests**

Create `tests/tasks.py`:

```python
from django.tasks import task


@task
def add(a, b):
    return a + b


@task
async def add_async(a, b):
    return a + b
```

Create `tests/test_enqueue.py`:

```python
import asyncio

import pytest
from django.core.management import call_command
from django.db import transaction
from django.tasks import TaskResultStatus
from django.tasks.exceptions import InvalidTask

from django_absurd.queues import get_absurd_client
from tests.tasks import add, add_async

pytestmark = pytest.mark.django_db(transaction=True)


def claim_one():
    return get_absurd_client().claim_tasks(batch_size=1)


def test_enqueue_lands_and_returns_taskresult():
    call_command("absurd_sync_queues")
    result = add.enqueue(1, 2)
    assert isinstance(result.id, str) and result.id
    assert result.status == TaskResultStatus.READY
    assert result.args == [1, 2]
    assert result.kwargs == {}
    assert result.backend == "default"
    claimed = claim_one()
    assert len(claimed) == 1
    assert claimed[0]["task_name"] == "tests.tasks.add"
    assert claimed[0]["params"] == {"args": [1, 2], "kwargs": {}}


def test_enqueue_preserves_kwargs():
    call_command("absurd_sync_queues")
    add.enqueue(a=1, b=2)
    assert claim_one()[0]["params"] == {"args": [], "kwargs": {"a": 1, "b": 2}}


def test_enqueue_rides_django_transaction():
    call_command("absurd_sync_queues")

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction.atomic():
        add.enqueue(1, 2)
        raise Boom
    assert claim_one() == []


def test_async_task_rejected():
    call_command("absurd_sync_queues")
    with pytest.raises(InvalidTask):
        add_async.enqueue(1, 2)


def test_undeclared_queue_rejected():
    call_command("absurd_sync_queues")
    with pytest.raises(InvalidTask):
        add.using(queue_name="nope").enqueue(1, 2)


def test_aenqueue_lands():
    call_command("absurd_sync_queues")
    asyncio.run(add.aenqueue(1, 2))
    assert len(claim_one()) == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `PGPORT=5433 uv run pytest tests/test_enqueue.py -v` Expected: FAIL — `enqueue`
raises `NotImplementedError` (SP1 stub) on the happy-path/transaction/aenqueue tests;
the rejection tests may also fail because `validate_task` is never reached. (If
`InvalidTask`'s import path differs, the implementer corrects it — it lives in
`django.tasks.exceptions`.)

- [ ] **Step 3: Implement minimal (prose — no production code block)**

In `django_absurd/backends.py`:

- Add the four class attributes `supports_get_result = False`,
  `supports_async_task = False`, `supports_defer = False`, `supports_priority = False`.
- Replace the `enqueue` `NotImplementedError` stub with the real method: call
  `self.validate_task(task)` first (base enforces module-level func, rejects coroutine
  funcs since `supports_async_task=False`, and checks `task.queue_name in self.queues`).
  Then get the sync client via `get_absurd_client(self.database)` and call
  `client.spawn(task.module_path, {"args": list(args), "kwargs": kwargs}, queue=task.queue_name, max_attempts=self.default_max_attempts)`.
  Construct and return a `TaskResult` with the fields listed in Global Constraints (`id`
  from `spawn["task_id"]`, `status=TaskResultStatus.READY`, `enqueued_at=timezone.now()`
  via `django.utils.timezone`, the rest as specified). Import
  `TaskResult`/`TaskResultStatus` from `django.tasks` and use `import typing as t` only
  where types are needed. Do NOT override `aenqueue` (base provides it). Leave
  `get_result`/`aget_result` unimplemented (the `supports_get_result=False` flag keeps
  Django from calling them).

- [ ] **Step 4: Run to verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_enqueue.py -v` Expected: PASS (6 tests). Run:
`PGPORT=5433 uv run pytest` Expected: whole single-DB suite green. Run:
`uv run ruff check django_absurd tests` Expected: All checks passed.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py tests/tasks.py tests/test_enqueue.py
git commit -m "feat: AbsurdBackend.enqueue -> spawn (sync produce side)"
```

---

### Task 2: Clear error when the target queue isn't provisioned

**Files:**

- Modify: `django_absurd/backends.py` (wrap the spawn error)
- Modify: `tests/test_enqueue.py` (add the test)

**Interfaces:**

- Consumes: `AbsurdBackend.enqueue` (Task 1).
- Produces: `enqueue` raises `django.core.exceptions.ImproperlyConfigured` with an
  actionable message (naming the queue + `absurd_sync_queues`) when the declared queue
  exists in the allowlist but is not provisioned in Absurd.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_enqueue.py`:

```python
from django.core.exceptions import ImproperlyConfigured


def test_enqueue_to_unprovisioned_queue_raises_clear_error():
    # No absurd_sync_queues: the "default" queue passes the allowlist
    # (it's in TASKS QUEUES) but was never created in Absurd.
    with pytest.raises(ImproperlyConfigured) as exc:
        add.enqueue(1, 2)
    message = str(exc.value)
    assert "default" in message
    assert "absurd_sync_queues" in message
```

- [ ] **Step 2: Run to verify it fails**

Run:
`PGPORT=5433 uv run pytest tests/test_enqueue.py::test_enqueue_to_unprovisioned_queue_raises_clear_error -v`
Expected: FAIL — the raw psycopg error from `absurd.spawn_task` (the per-queue table
`t_default` is absent) propagates instead of `ImproperlyConfigured`. Note the exact
exception type observed (e.g. `psycopg.errors.UndefinedTable`, a `ProgrammingError`
subclass) — that's what Step 3 catches.

- [ ] **Step 3: Implement minimal (prose)**

In `enqueue`, wrap the `client.spawn(...)` call: catch the database error that
`absurd.spawn_task` raises when the queue's backing table doesn't exist (the
`ProgrammingError`/`UndefinedTable` observed in Step 2) and re-raise
`ImproperlyConfigured` with a clear, actionable message that names `task.queue_name` and
tells the operator to run `manage.py absurd_sync_queues` (and `manage.py migrate` if the
schema itself is absent). Let unrelated errors propagate unchanged. Keep the catch tight
(only the provisioning case) so real bugs aren't masked.

- [ ] **Step 4: Run to verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_enqueue.py -v` Expected: PASS (7 tests). Run:
`PGPORT=5433 uv run pytest` Expected: whole single-DB suite green. Run:
`uv run ruff check django_absurd tests` Expected: All checks passed.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py tests/test_enqueue.py
git commit -m "feat: clear error when enqueueing to an unprovisioned queue"
```

---

## Self-Review

**Spec coverage:** support flags (T1); `enqueue` happy path + payload + TaskResult
shape + enqueue-on-commit + base `aenqueue` (T1); async/undeclared-queue rejection via
`validate_task` (T1); point-of-use clear error for unprovisioned queue (T2).
Out-of-scope items (result retrieval, native async, defer, priority) correctly absent.
All spec sections map to a task.

**Type consistency:** `enqueue(self, task, args, kwargs) -> TaskResult`, the
`supports_*` flags, the `{"args": …, "kwargs": …}` payload, and the claim-back
assertions (`task_name`, `params`) are used identically across T1–T2 and match the SP1
`AbsurdBackend.{alias,database,default_max_attempts}` interface.

**Placeholder scan:** no TBD/TODO; every test step has full assertion code;
implementation steps are prose per the project's no-coding-ahead rule. The two
exact-exception details (`InvalidTask` import path, the spawn `ProgrammingError`
subtype) are explicitly pinned at RED in Steps 2 rather than guessed.
