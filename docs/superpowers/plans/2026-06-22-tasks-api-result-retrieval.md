# Result Retrieval (get_result) (SP6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> Follow TDD: write the failing test, run it RED, implement the minimal code, run GREEN,
> refactor, commit.

**Goal:** Implement `AbsurdBackend.get_result` / `aget_result` (flip
`supports_get_result = True`) by reading Absurd's own per-queue task rows, with the
queue encoded into `TaskResult.id`.

**Architecture:** `enqueue` mints `result.id = f"{queue}:{task_id}"`.
`get_result(result_id)` decodes `(queue, task_id)`, raw-SQL reads `absurd.t_<queue>`
(left join `absurd.r_<queue>`) through the Django connection, and reconstructs a full
`TaskResult`. No new model, no migration, no extra write.

**Tech Stack:** Django 6.0 `django.tasks`, absurd-sdk, psycopg3
(`psycopg.sql.Identifier`), pytest + pytest-django, real Postgres.

## Global Constraints

- `import typing as t` — never `from typing import X`. Absolute imports only. Helpers
  BELOW the public functions that use them. No leading-underscore module names. Helper
  functions need a verb (e.g. `decode_result_id`, `read_task_row`, `build_task_result`).
- ruff `select=["ALL"]` passes with ZERO new ignores/noqa (HARD RULE — ask before adding
  any). `ANN` is already ignored under `tests/**`. mypy (django-stubs) passes with NO
  `# type: ignore`.
- pytest, function-based ONLY, NO mocks. `tests/test_enqueue.py` /
  `tests/test_worker.py` use `pytestmark = pytest.mark.django_db(transaction=True)`. New
  result tests need the same (worker commits + DDL).
- **Untrusted queue:** `queue` is parsed from a caller-supplied `result_id`. Build table
  identifiers ONLY with `psycopg.sql.Identifier("absurd", f"t_{queue}")` (two-arg →
  supplies the `absurd` schema; the SQL template must NOT also write a literal `absurd.`
  prefix). NEVER use `connection.ops.quote_name` (no embedded-quote escaping) or
  f-string interpolation.
- **Errors through Django's cursor:** a missing table surfaces as
  `django.db.utils.ProgrammingError` (NOT raw `psycopg.errors.UndefinedTable`). Wrap the
  read in `transaction.atomic(using=self.database, savepoint=True)` so a failed lookup
  can't poison an enclosing `atomic()`.
- **jsonb:** Django's psycopg3 backend does NOT decode these jsonb columns; call
  `register_jsonb_loader(connection.connection)` (existing helper in
  `django_absurd/queues.py`) after `connection.ensure_connection()` and before execute;
  nothing between may reconnect.
- Status map: `pending→READY`, `running→RUNNING`, `sleeping→RUNNING`,
  `completed→SUCCESSFUL`, `failed→FAILED`, `cancelled→FAILED`.
- DB: `PGPORT=5433 docker compose up -d db`; run pytest with `PGPORT=5433`.

Spec: `docs/superpowers/specs/2026-06-22-tasks-api-result-retrieval-design.md`.

---

### Task 1: Encode queue in `result.id` (+ keep the worker suite green)

**Files:**

- Modify: `django_absurd/backends.py` (the `id=str(spawn_result["task_id"])` line in
  `enqueue`'s returned `TaskResult`, currently `django_absurd/backends.py:61`)
- Modify: `tests/test_worker.py` (the `snapshot` helper at `tests/test_worker.py:84`)
- Modify: `tests/test_enqueue.py` (add the id-format test)

**Interfaces:**

- Produces: `TaskResult.id == f"{task.queue_name}:{task_id}"`. The trailing 36-char
  segment after the last `:` is the raw Absurd `task_id` uuid. `get_result` (Task 2)
  consumes this.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_enqueue.py` (module already has `pytestmark` + imports `add`):

```python
def test_result_id_encodes_queue():
    call_command("absurd_sync_queues")
    result = add.enqueue(1, 2)
    queue, _, task_id = result.id.rpartition(":")
    assert queue == "default"
    assert task_id  # the raw Absurd uuid
    # the claimed row's task_id matches the encoded suffix
    assert claim_one()[0]["task_id"] == task_id
```

- [ ] **Step 2: Run to verify it fails**

Run: `PGPORT=5433 uv run pytest tests/test_enqueue.py::test_result_id_encodes_queue -v`
Expected: FAIL — `result.id` is currently the raw uuid (`str(task_id)`), so
`result.id.rpartition(":")` yields `queue == ""` (no colon), assertion fails.

- [ ] **Step 3: Implement minimal (prose — no production code block)**

- In `django_absurd/backends.py`, `enqueue`: change the returned `TaskResult`'s `id=`
  from `str(spawn_result["task_id"])` to an f-string encoding the queue:
  `f"{task.queue_name}:{spawn_result['task_id']}"`. Nothing else in `enqueue` changes.
- In `tests/test_worker.py`, make the
  `snapshot(task_id, alias="default", queue="default")` helper tolerate the new
  composite id: at the top of the function, normalize
  `task_id = task_id.rsplit(":", 1)[-1]` before calling `client.fetch_task_result(...)`.
  `rsplit(":", 1)[-1]` returns the raw uuid for a `"queue:uuid"` id AND is a no-op for a
  bare uuid (the `snapshot(spawn["task_id"])` call at `tests/test_worker.py:144` passes
  a raw uuid — still works).

- [ ] **Step 4: Run to verify it passes**

Run: `PGPORT=5433 uv run pytest tests/test_enqueue.py::test_result_id_encodes_queue -v`
→ PASS. Run: `PGPORT=5433 uv run pytest tests/test_worker.py tests/test_enqueue.py -v` →
all green (the `snapshot` fix keeps the 6 `snapshot(result.id)` sites working; the SP5
idempotency test `r1.id == r2.id` still holds — same queue+task_id).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py tests/test_worker.py tests/test_enqueue.py
git commit -m "feat: encode queue in TaskResult.id (queue:task_id) for result retrieval"
```

---

### Task 2: `get_result` / `aget_result`

**Files:**

- Modify: `django_absurd/backends.py` (`supports_get_result = True`; implement
  `get_result`, `aget_result`; add module-level helpers BELOW the class)
- Create: `tests/test_results.py`

**Interfaces:**

- Consumes: the `queue:task_id` id from Task 1; `register_jsonb_loader(raw_conn)` from
  `django_absurd/queues.py`; `django.db.connections`, `django.db.transaction`;
  `psycopg.sql`; `psycopg.errors`; `django.db.utils.ProgrammingError`;
  `django.utils.module_loading.import_string`; `django.tasks.TaskResult`,
  `TaskResultStatus`, `django.tasks.base.TaskError`;
  `django.tasks.exceptions.TaskResultDoesNotExist`;
  `django.core.exceptions.ImproperlyConfigured`.
- Produces: `get_result(self, result_id: str) -> TaskResult`. `aget_result` is provided
  by `BaseTaskBackend` once `get_result` exists (the existing NotImplementedError
  override is DELETED) — no new method written here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_results.py`:

```python
import asyncio
import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import transaction
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist

from django_absurd.params import AbsurdSpawnParams
from django_absurd.queues import get_absurd_backends, get_absurd_client
from tests.tasks import add, boom

pytestmark = pytest.mark.django_db(transaction=True)


def backend():
    return get_absurd_backends()["default"]


def run_absurd_worker(queue="default"):
    call_command("absurd_worker", queue=queue, burst=True)


def test_get_result_pending():
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    got = backend().get_result(r.id)
    assert got.id == r.id
    assert got.status == TaskResultStatus.READY
    assert got.args == [2, 3]
    assert got.kwargs == {}
    assert got.enqueued_at is not None
    assert got.task.module_path == "tests.tasks.add"


def test_get_result_successful():
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    run_absurd_worker()
    got = backend().get_result(r.id)
    assert got.status == TaskResultStatus.SUCCESSFUL
    assert got.return_value == 5
    assert got.finished_at is not None
    assert got.last_attempted_at is not None
    assert got.worker_ids  # non-empty


def test_refresh_round_trip():
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    run_absurd_worker()
    r.refresh()
    assert r.status == TaskResultStatus.SUCCESSFUL
    assert r.return_value == 5


def test_get_result_failed_has_errors():
    call_command("absurd_sync_queues")
    r = boom.enqueue(absurd_spawn_params=AbsurdSpawnParams(max_attempts=1))
    run_absurd_worker()
    got = backend().get_result(r.id)
    assert got.status == TaskResultStatus.FAILED
    assert len(got.errors) == 1
    assert "ValueError" in got.errors[0].exception_class_path
    assert got.errors[0].traceback


def test_via_task_get_result():
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    got = add.get_result(r.id)  # public path; must not raise TaskResultMismatch
    assert got.id == r.id


def test_unknown_id_raises_does_not_exist():
    call_command("absurd_sync_queues")
    with pytest.raises(TaskResultDoesNotExist):
        backend().get_result(f"default:{uuid.uuid4()}")


def test_malformed_id_raises_does_not_exist():
    call_command("absurd_sync_queues")
    with pytest.raises(TaskResultDoesNotExist):
        backend().get_result("nocolon")


def test_get_result_inside_atomic_does_not_poison_txn():
    call_command("absurd_sync_queues")
    with transaction.atomic():
        with pytest.raises(TaskResultDoesNotExist):
            backend().get_result(f"default:{uuid.uuid4()}")
        # the outer transaction must still be usable (savepoint rolled back)
        assert get_absurd_client().list_queues()  # any ORM/DB op succeeds


def test_removed_task_raises_improperly_configured():
    call_command("absurd_sync_queues")
    # spawn a row whose task_name does not import
    spawn = get_absurd_client().spawn(
        "tests.tasks.does_not_exist", {"args": [], "kwargs": {}}, queue="default"
    )
    rid = f"default:{spawn['task_id']}"
    with pytest.raises(ImproperlyConfigured):
        backend().get_result(rid)


def test_injection_in_queue_segment_is_safe():
    call_command("absurd_sync_queues")
    evil = 'default"; drop table absurd.queues; --'
    with pytest.raises(TaskResultDoesNotExist):
        backend().get_result(f"{evil}:{uuid.uuid4()}")
    # the queues table still exists
    assert "default" in get_absurd_client().list_queues()


def test_aget_result_matches_get_result():
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    got = asyncio.run(backend().aget_result(r.id))
    assert got.id == r.id
    assert got.status == TaskResultStatus.READY
```

- [ ] **Step 2: Run to verify they fail**

Run: `PGPORT=5433 uv run pytest tests/test_results.py -v` Expected: FAIL —
`AbsurdBackend.get_result` currently raises `NotImplementedError` (and
`supports_get_result = False`), so every test errors.

- [ ] **Step 3: Implement minimal (prose — no production code block)**

In `django_absurd/backends.py`:

- Set `supports_get_result = True` on `AbsurdBackend`.
- Implement `get_result(self, result_id)`:
  1. Decode via a helper `decode_result_id(result_id)` → `(queue, task_id)` using
     `result_id.rsplit(":", 1)`; if there's no `":"` (single element), raise
     `TaskResultDoesNotExist`.
  2. If `self` has a declared queue set (`getattr(self, "queues", None)`) and `queue` is
     not in it, raise `TaskResultDoesNotExist` (early whitelist reject).
  3. `connection = connections[self.database]`; `connection.ensure_connection()`;
     `register_jsonb_loader(connection.connection)`.
  4. Build the SELECT with
     `psycopg.sql.SQL(template).format(t=psycopg.sql.Identifier("absurd", f"t_{queue}"), r=psycopg.sql.Identifier("absurd", f"r_{queue}"))`
     — template uses `{t}` / `{r}` placeholders and NO literal `absurd.` prefix. The
     SELECT is the one from the spec (task_name, params, enqueue_at, first_started_at,
     state, completed_payload, cancelled_at;
     `lr.started_at`/`completed_at`/`failed_at`/`failure_reason` via
     `LEFT JOIN {r} lr ON lr.run_id = t.last_attempt_run`; `worker_ids` via
     `(SELECT array_agg(r.claimed_by ORDER BY r.attempt) FROM {r} r WHERE r.task_id = t.task_id AND r.claimed_by IS NOT NULL)`).
     Render with `.as_string(connection.connection)`.
  5. Run inside
     `with transaction.atomic(using=self.database, savepoint=True): cursor = connection.cursor(); cursor.execute(rendered, [task_id]); row = cursor.fetchone()`.
     Catch `django.db.utils.ProgrammingError` → raise `TaskResultDoesNotExist`.
     (VERIFIED live: through Django's wrapper with a fully-qualified table ref, BOTH a
     missing queue table AND a dropped `absurd` schema surface as `ProgrammingError`
     cause `UndefinedTable` `42P01` — there is no separate `InvalidSchemaName`/`3F000`
     to branch on, so do NOT special-case schema-absent here; the queue whitelist
     already gates real ids. `enqueue`'s `ImproperlyConfigured` path is unrelated — it
     catches raw `psycopg.errors.*` off the SDK's own cursor.) The savepoint keeps the
     outer transaction usable after the catch.
  6. `row is None` → raise `TaskResultDoesNotExist`.
  7. Build the `TaskResult` via a helper
     `build_task_result(self, result_id, queue, row)` (see below).
- Helper `build_task_result(self, result_id, queue, row)` (module-level function BELOW
  the class, or a small private-but-verb-named method — prefer module-level verb-named):
  - `task = import_string(task_name)`; wrap the `import_string` in
    `try/except ImportError: raise ImproperlyConfigured(f"task '{task_name}' is no longer importable")`.
    If `task.queue_name != queue`, `task = task.using(queue_name=queue)`.
  - `status` via a helper `map_state_to_status(state)` (the status map from Global
    Constraints; default/unknown → `READY`).
  - `errors`: when `state == "failed"` and `failure_reason` present,
    `[TaskError(exception_class_path=failure_reason.get("name", ""), traceback=failure_reason.get("traceback") or failure_reason.get("message", ""))]`;
    else `[]`.
  - `finished_at = completed_at or failed_at or cancelled_at`;
    `last_attempted_at = run_started`; `started_at = first_started_at`;
    `enqueued_at = enqueue_at`; `worker_ids = worker_ids_array or []`.
  - Construct
    `TaskResult(task=task, id=result_id, status=status, enqueued_at=…, started_at=…, finished_at=…, last_attempted_at=…, args=params["args"], kwargs=params["kwargs"], backend=self.alias, errors=errors, worker_ids=worker_ids)`.
  - If `state == "completed"`, set the return value AFTER construction:
    `object.__setattr__(result, "_return_value", completed_payload)` (it's
    `field(init=False)` on a frozen dataclass).
  - Return `result`.
- `aget_result`: DELETE the existing `aget_result` override (currently
  `django_absurd/backends.py:77-78`, raising `NotImplementedError`). `BaseTaskBackend`
  already provides a working `aget_result` = `sync_to_async(self.get_result)`, so once
  `get_result` is implemented the base handles async. Do NOT add a new override.
  (`get_result` itself replaces the current NotImplementedError body at
  `django_absurd/backends.py:74-75`.)
- Keep imports tidy: add the runtime imports needed —
  `from django.tasks.base import TaskError`,
  `from django.tasks.exceptions import TaskResultDoesNotExist`,
  `from django.utils.module_loading import import_string`,
  `from django.db import connections, transaction`, and `import psycopg.sql` (the file
  has only `import psycopg.errors` today; `psycopg.sql.SQL`/`Identifier` need the `sql`
  submodule). `ImproperlyConfigured` is already imported (used by the removed-task
  `ImportError` path). `sync_to_async` is NOT needed (base handles aget_result).
  `import typing as t` already present.
- **Lint watch (no new noqa):** keep `decode_result_id` / `map_state_to_status` /
  `build_task_result` as separate module-level verb-named helpers so `get_result` stays
  under ruff's `PLR0911` (≤6 returns/raises) / `PLR0912` (≤12 branches) — with the
  single `ProgrammingError → TaskResultDoesNotExist` except (above), this is comfortably
  clean. Run ruff after; if a count trips, extract a helper — do NOT add an ignore.

- [ ] **Step 4: Run to verify they pass**

Run: `PGPORT=5433 uv run pytest tests/test_results.py -v` → all pass.

- [ ] **Step 5: Full suite + gates**

Run: `PGPORT=5433 uv run pytest` → full single-DB suite green. Run:
`PGPORT=5433 uv run pytest tests/multidb` → green. Run:
`uv run ruff check django_absurd tests` → clean (no new noqa). Run:
`uv run mypy django_absurd` → Success.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/backends.py tests/test_results.py
git commit -m "feat: implement get_result/aget_result (raw-SQL over Absurd tables)"
```

---

## Self-Review

**Spec coverage:** id-encode (Task 1); `supports_get_result=True` + decode + whitelist +
jsonb loader + savepoint + identifier-safe SQL + Django-wrapped error handling +
reconstruction mapping + status map + errors→TaskError + `_return_value` via
`object.__setattr__` + `aget_result` (Task 2 Step 3); observability tests for
pending/successful/failed + refresh round-trip + unknown/malformed + atomic-no-poison +
removed-task + injection + aget + via-Task.get*result (Task 2 Step 1); worker-suite
blast radius via the `snapshot` rsplit fix (Task 1). `cancellation` has no read path —
not asserted (spec). `worker_ids` non-distinct so `.attempts` stays consistent (Task 2
SQL). The best-effort `finished_at`/`last_attempted_at` come from `r*<queue>` (Task 2
mapping).

**Placeholder scan:** none — every test step carries full assertion code; implementation
steps are prose (no production-code blocks) per the no-coding-ahead rule.

**Type consistency:** `decode_result_id`/`build_task_result`/`map_state_to_status`
helper names match between the implementation prose and the field mapping; `result.id`
format produced in Task 1 (`queue:task_id`) is the exact input `decode_result_id`
consumes in Task 2; `TaskError(exception_class_path=, traceback=)` and `TaskResult(...)`
kwargs match the verified Django signatures; `register_jsonb_loader(raw_conn)` matches
`queues.py`.
