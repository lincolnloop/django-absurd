# Absurd Spawn Params (SP5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let callers thread Absurd `spawn` params through `AbsurdBackend.enqueue` — a
per-task default decorator and a per-enqueue override kwarg — folded into one `spawn`
call.

**Architecture:** New `django_absurd/params.py` holds two frozen dataclasses
(`AbsurdDefaultParams` → its subclass `AbsurdSpawnParams`) + the
`@absurd_default_params` decorator (attaches the default to the task FUNCTION).
`AbsurdBackend.enqueue` reads the decorator default off `task.func`, pops the reserved
`absurd_spawn_params` kwarg, merges (per-call over default), and splats the result into
`client.spawn`. The worker is NOT touched.

**Tech Stack:** Django 6.0 `django.tasks`, absurd-sdk, psycopg3, pytest + pytest-django,
real Postgres.

## Global Constraints

- Floor Django 6.0 / Python 3.12; psycopg3 backend; targets `DATABASES['default']`.
- `import typing as t` — never `from typing import X`. Absolute imports only. Helpers
  BELOW the public functions that use them. No leading-underscore module names.
  Functions should contain a verb — EXCEPT the user-chosen public names here:
  `to_kwargs`, `absurd_default_params` (decorator), `AbsurdDefaultParams`,
  `AbsurdSpawnParams` (kept to mirror Absurd's vocabulary). Do not rename them.
- ruff `select=["ALL"]` must pass with NO new ignores/noqa (hard rule — ask before
  adding any). `ANN` is already ignored under `tests/**`, so test code needs no
  annotations; production code in `params.py`/`backends.py` DOES.
- mypy (django-stubs) must pass with no `# type: ignore`.
- Field types are the exact Absurd types:
  `from absurd_sdk import RetryStrategy, CancellationPolicy, JsonObject`.
  `NOT_SET: t.Any = object()` sentinel; `to_kwargs()` emits only fields `is not NOT_SET`
  (unset → Absurd default stands). No `| None` in field types.
- The decorator's `isinstance` order-check needs a RUNTIME
  `from django.tasks import Task` (NOT a `TYPE_CHECKING`-only import).
- Tests: pytest, function-based, NO mocks/`unittest.mock`, no `responses` needed. Lean
  INTEGRATION — drive `task.enqueue(...)` against real Postgres and assert via
  `client.claim_tasks` (`ClaimedTask` exposes `max_attempts`, `headers`,
  `retry_strategy`; NOT `cancellation`). `cancellation` has no public read path.
  Existing `tests/test_enqueue.py` uses
  `pytestmark = pytest.mark.django_db(transaction=True)` and a `claim_one()` helper →
  `get_absurd_client().claim_tasks(batch_size=1)`. A claim CONSUMES the row — dedup and
  claim-inspection tests must use distinct tasks/keys.
- DB: `PGPORT=5433 docker compose up -d db` (export `PGPORT` — bare `docker compose up`
  maps host 5432 and can collide); run pytest with `PGPORT=5433`.
- Precedence per key: per-call `AbsurdSpawnParams` > `@absurd_default_params` default >
  (for `max_attempts`) backend `OPTIONS["DEFAULT_MAX_ATTEMPTS"]` (current default 5).
  `headers`/`idempotency_key` only ever come from the per-call layer.

Spec: `docs/superpowers/specs/2026-06-22-tasks-api-spawn-options-design.md`.

---

### Task 1: `params.py` — dataclasses + `@absurd_default_params` decorator

**Files:**

- Create: `django_absurd/params.py`
- Create: `tests/test_params.py`

**Interfaces:**

- Consumes: `from absurd_sdk import RetryStrategy, CancellationPolicy, JsonObject`;
  `from django.tasks import Task`, `task`.
- Produces:

  - `NOT_SET: t.Any` (module sentinel).
  - `AbsurdDefaultParams` (frozen dataclass): `max_attempts: int = NOT_SET`,
    `retry_strategy: RetryStrategy = NOT_SET`,
    `cancellation: CancellationPolicy = NOT_SET`; `to_kwargs(self) -> dict[str, t.Any]`.
  - `AbsurdSpawnParams(AbsurdDefaultParams)`: adds `headers: JsonObject = NOT_SET`,
    `idempotency_key: str = NOT_SET`.
  - `absurd_default_params(**kwargs: t.Any) -> t.Callable[[t.Any], t.Any]` — decorator
    factory; builds `AbsurdDefaultParams(**kwargs)`, returns a decorator that sets
    `func.absurd_default_params` on the raw function and returns it; raises `TypeError`
    if handed a `Task` (wrong order).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_params.py`:

```python
import pytest
from django.tasks import task

from django_absurd.params import (
    AbsurdDefaultParams,
    AbsurdSpawnParams,
    absurd_default_params,
)
from tests.tasks import add


# Module-level task: Django's validate_task rejects functions whose __qualname__
# contains "<locals>" (raises InvalidTask at decoration), so a @task defined inside
# a test body fails BEFORE our decorator's Task-guard runs. Declare it at module level.
@task
@absurd_default_params(max_attempts=7)
def good_default(a, b):
    return a + b


def test_to_kwargs_emits_only_set_fields():
    # to_kwargs is OUR serializer: unset fields omitted so Absurd's defaults stand.
    assert AbsurdSpawnParams(max_attempts=3).to_kwargs() == {"max_attempts": 3}
    assert AbsurdSpawnParams().to_kwargs() == {}


def test_spawnparams_carries_per_invocation_fields():
    params = AbsurdSpawnParams(idempotency_key="k", headers={"x": "1"})
    assert params.to_kwargs() == {"idempotency_key": "k", "headers": {"x": "1"}}


def test_decorator_rejects_per_invocation_kwarg():
    # the decorator builds AbsurdDefaultParams, which has no per-invocation field
    with pytest.raises(TypeError):
        absurd_default_params(idempotency_key="k")


def test_decorator_attaches_default_to_task_func():
    assert good_default.func.absurd_default_params == AbsurdDefaultParams(max_attempts=7)


def test_decorator_above_task_raises():
    # applied above @task the decorator receives a Task (not a function) -> TypeError
    # (`add` is a module-level Task)
    with pytest.raises(TypeError):
        absurd_default_params(max_attempts=7)(add)
```

> NOTE (impl-time correction): a `@task` cannot be defined inside a test function body —
> `validate_task`/`is_module_level_function` rejects the `<locals>` qualname with
> `InvalidTask` before our `isinstance(func, Task)` guard runs. So the attach test uses
> a module-level task, and the wrong-order test hands the decorator a real module-level
> `Task` (`tests.tasks.add`) directly. The `params.py` implementation is unchanged by
> this.

(Dropped pure-dataclass-mechanics tests — `NOT_SET` singleton identity and the
standalone `AbsurdDefaultParams(...)`-constructor rejection — per "don't test the
framework." The serializer (`to_kwargs`) and our decorator behavior, including subset
rejection via the decorator, remain covered.)

- [ ] **Step 2: Run to verify they fail**

Run: `PGPORT=5433 uv run pytest tests/test_params.py -v` Expected: FAIL —
`ModuleNotFoundError: No module named 'django_absurd.params'` (nothing implemented yet).

- [ ] **Step 3: Implement `django_absurd/params.py` (minimal — prose, no finished code
      block)**

- Module imports: `import dataclasses`, `import typing as t`,
  `from absurd_sdk import CancellationPolicy, RetryStrategy, JsonObject`,
  `from django.tasks import Task` (runtime import — needed for the `isinstance` check).
- Define the sentinel: `NOT_SET: t.Any = object()` (annotating it `t.Any` lets
  `x: int = NOT_SET` type-check with no ignore).
- Define `AbsurdDefaultParams` as a `@dataclasses.dataclass(frozen=True)` with a class
  docstring, three fields each defaulted to `NOT_SET` and typed with the exact Absurd
  type (`max_attempts: int`, `retry_strategy: RetryStrategy`,
  `cancellation: CancellationPolicy`). Add method `to_kwargs(self) -> dict[str, t.Any]`
  returning
  `{f.name: getattr(self, f.name) for f in dataclasses.fields(self) if getattr(self, f.name) is not NOT_SET}`.
- Define `AbsurdSpawnParams(AbsurdDefaultParams)` (also
  `@dataclasses.dataclass(frozen=True)`, docstring) adding
  `headers: JsonObject = NOT_SET` and `idempotency_key: str = NOT_SET`. It inherits
  `to_kwargs` (which iterates `dataclasses.fields(self)`, so it picks up the subclass
  fields too).
- Define the decorator factory
  `absurd_default_params(**kwargs: t.Any) -> t.Callable[[t.Any], t.Any]` BELOW the
  dataclasses: build `params = AbsurdDefaultParams(**kwargs)` (an unknown /
  per-invocation kwarg raises `TypeError` here — the only validation needed). Define an
  inner `set_default(func: t.Any) -> t.Any`: if `isinstance(func, Task)` raise
  `TypeError("apply @absurd_default_params below @task, not above it")`; else set
  `func.absurd_default_params = params` and `return func`. Return `set_default`.

- [ ] **Step 4: Run to verify they pass**

Run: `PGPORT=5433 uv run pytest tests/test_params.py -v` Expected: PASS (all 7).

- [ ] **Step 5: Gates**

Run: `uv run ruff check django_absurd/params.py tests/test_params.py` → clean (no new
noqa). Run: `uv run mypy django_absurd` → Success (no new `type: ignore`).

- [ ] **Step 6: Commit**

```bash
git add django_absurd/params.py tests/test_params.py
git commit -m "feat: AbsurdDefaultParams/AbsurdSpawnParams + @absurd_default_params decorator"
```

---

### Task 2: Thread params through `AbsurdBackend.enqueue`

**Files:**

- Modify: `django_absurd/backends.py` (the `enqueue` method, currently
  `django_absurd/backends.py:34-72`)
- Modify: `tests/tasks.py` (add one task with a decorator default)
- Modify: `tests/test_enqueue.py` (add integration tests)

**Interfaces:**

- Consumes: `AbsurdDefaultParams`/`AbsurdSpawnParams`/`absurd_default_params` (Task 1);
  `client.spawn(task_name, params, *, queue, max_attempts, retry_strategy, headers, cancellation, idempotency_key)`;
  `get_absurd_client(self.database).claim_tasks(batch_size=...)` → `ClaimedTask` dicts
  with `max_attempts`/`headers`/`retry_strategy`.
- Produces: `enqueue` that pops `absurd_spawn_params` from `kwargs` and merges
  decorator-default + per-call into the `spawn` call. (No signature change —
  `absurd_spawn_params` rides in `kwargs`.)

- [ ] **Step 1: Add the fixture task + write the failing tests**

Add to `tests/tasks.py` (it already imports `task`); add the new import at top:

```python
from django_absurd.params import absurd_default_params
```

and the task at the end:

```python
@task
@absurd_default_params(max_attempts=7)
def with_default_attempts(a, b):
    return a + b
```

Add to `tests/test_enqueue.py` — extend the imports:

```python
from django_absurd.params import AbsurdSpawnParams
from tests.tasks import add, add_async, with_default_attempts
```

and append these tests (they use the existing `claim_one()` helper + module
`pytestmark`):

```python
def test_max_attempts_uses_backend_default_when_unset():
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)
    assert claim_one()[0]["max_attempts"] == 5  # backend DEFAULT_MAX_ATTEMPTS


def test_max_attempts_uses_decorator_default():
    call_command("absurd_sync_queues")
    with_default_attempts.enqueue(1, 2)
    assert claim_one()[0]["max_attempts"] == 7  # @absurd_default_params(max_attempts=7)


def test_per_call_max_attempts_overrides_decorator_and_backend():
    call_command("absurd_sync_queues")
    with_default_attempts.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(max_attempts=9))
    assert claim_one()[0]["max_attempts"] == 9  # per-call wins


def test_headers_reach_spawn():
    call_command("absurd_sync_queues")
    add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(headers={"trace": "abc"}))
    assert claim_one()[0]["headers"] == {"trace": "abc"}


def test_retry_strategy_reaches_spawn():
    call_command("absurd_sync_queues")
    strategy = {"kind": "fixed", "base_seconds": 1.0, "factor": 2.0, "max_seconds": 10.0}
    add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(retry_strategy=strategy))
    assert claim_one()[0]["retry_strategy"] == strategy


def test_idempotency_key_dedups():
    call_command("absurd_sync_queues")
    r1 = add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="dup"))
    r2 = add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="dup"))
    assert r1.id == r2.id
    assert len(get_absurd_client().claim_tasks(batch_size=10)) == 1


def test_spawn_params_not_passed_to_task_func():
    call_command("absurd_sync_queues")
    add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="x"))
    # the reserved kwarg is popped before building params -> never reaches the task
    assert claim_one()[0]["params"] == {"args": [1, 2], "kwargs": {}}
```

- [ ] **Step 2: Run to verify they fail**

Run:
`PGPORT=5433 uv run pytest tests/test_enqueue.py -v -k "max_attempts or headers or retry_strategy or idempotency or spawn_params_not"`
Expected: FAIL. Current `enqueue` ignores `absurd_spawn_params` — it stays in `kwargs`
and gets folded into the task `params` payload, so any params-bearing test (`headers`,
`retry_strategy`, `idempotency`, `per_call_max_attempts`,
`spawn_params_not_passed_to_task_func`) fails EARLY with
`TypeError: Object of type AbsurdSpawnParams is not JSON serializable` (raised by
`client.spawn`'s `json.dumps` of the params dict).
`test_max_attempts_uses_decorator_default` fails on the assertion (`max_attempts` is
hardcoded 5, not 7). `AbsurdSpawnParams` imports fine (Task 1 merged). NOTE:
`test_max_attempts_uses_backend_default_when_unset` PASSES both before and after — it
carries no params and pins the unchanged no-params path
(`max_attempts=self.default_max_attempts`=5). That's intended; it's a regression guard,
not a RED test.

- [ ] **Step 3: Implement the merge in `enqueue` (minimal — prose, no finished code
      block)**

In `django_absurd/backends.py`, inside `enqueue`, BEFORE the `client.spawn(...)` call:

- Pop the reserved kwarg out of `kwargs` so it cannot leak into the task payload:
  `spawn_params = kwargs.pop("absurd_spawn_params", None)`.
- Read the per-task default off the function:
  `defaults = getattr(task.func, "absurd_default_params", None)`.
- Build the merged option dict, per-call winning per key: start from
  `defaults.to_kwargs()` (or `{}` if `defaults` is `None`), then update with
  `spawn_params.to_kwargs()` (or `{}` if `None`). A short local helper or a two-line
  dict-merge is fine; keep it readable.
- Resolve `max_attempts` with the existing backend fallback:
  `max_attempts = merged.pop("max_attempts", self.default_max_attempts)`.
- Change the `client.spawn(...)` call so it passes `max_attempts=max_attempts` and
  splats the remaining merged options: `**merged` (these are
  `retry_strategy`/`headers`/`cancellation`/`idempotency_key` when set). Keep
  `task.module_path`, the `{"args": ..., "kwargs": ...}` params dict, and
  `queue=task.queue_name` exactly as now. Because `absurd_spawn_params` was popped, the
  params dict (`{"args": list(args), "kwargs": dict(kwargs)}`) and the returned
  `TaskResult(kwargs=dict(kwargs))` no longer contain it — no extra change needed there.
- The existing
  `try/except (UndefinedTable/UndefinedFunction/InvalidSchemaName) -> ImproperlyConfigured`
  must still wrap the `spawn` call unchanged.
- No new module-level import is required (read via `getattr`, pop via `dict.pop`); do
  NOT import the param classes into `backends.py`.

- [ ] **Step 4: Run to verify they pass**

Run: `PGPORT=5433 uv run pytest tests/test_enqueue.py -v` Expected: PASS — all new tests
plus the pre-existing enqueue tests (no-params path unchanged:
`test_enqueue_lands_and_returns_taskresult`, `test_enqueue_preserves_kwargs`,
`test_enqueue_rides_django_transaction`, etc.).

- [ ] **Step 5: Full suite + gates**

Run: `PGPORT=5433 uv run pytest` → full single-DB suite green. Run:
`PGPORT=5433 uv run pytest tests/multidb` → green. Run:
`uv run ruff check django_absurd tests` → clean (no new noqa;
`grep -rn noqa django_absurd` unchanged from before this branch). Run:
`uv run mypy django_absurd` → Success.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/backends.py tests/tasks.py tests/test_enqueue.py
git commit -m "feat: thread Absurd spawn params through enqueue (decorator default + per-call override)"
```

---

## Self-Review

**Spec coverage:** `AbsurdDefaultParams`/`AbsurdSpawnParams` + `NOT_SET` + `to_kwargs`
(Task 1, Step 3); `@absurd_default_params` attach + wrong-order `TypeError` +
per-invocation rejection (Task 1 tests + Step 3); enqueue pop + merge + precedence +
splat (Task 2, Step 3); observability via `claim_tasks`/`ClaimedTask` for
`max_attempts`/`headers`/`retry_strategy` (Task 2 tests); idempotency dedup via same
`task_id` + single row (Task 2); reserved-kwarg isolation via `params` claim (Task 2).
`cancellation` has no public read path — covered only at `to_kwargs` level (Task 1's
`test_to_kwargs_emits_only_set_fields` exercises the same code path; an explicit
`cancellation` claim assertion is intentionally omitted, matching the spec). The
best-effort worker+caplog retry test from the spec is intentionally NOT included — the
claim-based precedence test
(`test_per_call_max_attempts_overrides_decorator_and_backend`) is the authoritative
proof, per the user (don't test the framework; the claim assertion already pins
precedence).

**Placeholder scan:** no TBD/TODO; every test step has full assertion code;
implementation steps are prose (no production-code blocks) per the no-coding-ahead rule.

**Type consistency:**
`AbsurdDefaultParams`/`AbsurdSpawnParams`/`absurd_default_params`/`to_kwargs`/`NOT_SET`
names match between Task 1 (produced) and Task 2 (consumed);
`func.absurd_default_params` attribute name matches between the decorator (Task 1) and
the `getattr` read (Task 2); `absurd_spawn_params` reserved kwarg matches between the
tests and the `kwargs.pop` (Task 2); claim keys
(`max_attempts`/`headers`/`retry_strategy`/`params`) match the verified `ClaimedTask`
shape.
