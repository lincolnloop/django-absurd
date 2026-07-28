# Typed `absurd_params` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `@absurd_default_params` + the `absurd_spawn_params=` enqueue kwarg
with one `absurd_params(...)` factory usable as a decorator (below `@task`) or as
`absurd_params(...).bind(task).enqueue(...)`, typed end-to-end and free of
kwargs-namespace squatting.

**Architecture:** Params ride a real dataclass **field** on `AbsurdTask`, a frozen
`django.tasks.base.Task` subclass registered as `AbsurdBackend.task_class`.
`Task.using()` is `dataclasses.replace()`, so a field survives routing changes,
`deepcopy`, and pickling (a `__slots__` entry would not). The decorator writes a
function attribute (no task exists yet); `AbsurdTask.__post_init__` folds it into the
field with setdefault semantics, so `enqueue` and pg_cron `reconcile` read one place.
Static separation of the two call sites comes from an overload pair returning sibling
classes; every rule also raises a curated runtime `TypeError` because type checking is
optional for our users.

**Spec:**
[`docs/specs/2026-07-27-typed-absurd-params.md`](../specs/2026-07-27-typed-absurd-params.md)
— read it before starting. Tracked in
[#119](https://github.com/lincolnloop/django-absurd/issues/119).

## Amendments after execution

This plan was executed as written; two curated messages then changed during the
pre-merge revdiff. **The spec is correct; the message text quoted below is not.**

- **`run_after` no longer gets a curated pointer** (the message quoted in Task 2's
  `test_run_after_is_rejected_with_the_defer_pointer` is gone). It is not an Absurd
  spawn option — `spawn` takes `max_attempts`, `retry_strategy`, `headers`, `queue`,
  `cancellation`, `idempotency_key` — it is purely a `Task.using()` kwarg, so it was
  special-cased on a false premise. It now falls through to the generic invalid-argument
  message, and the test is `test_a_django_only_option_gets_no_special_pointer`. Used
  properly it raises Django's own `InvalidTask`, since `supports_defer = False`.
- **The routing message and docs say "Django's Task API"**, not bare "Django" — the
  distinction matters when a whole other API is in play.

Also: Task 2's per-invocation-as-decorator test now applies `@absurd_params(...)` with
real decorator syntax inside `pytest.raises`, rather than calling the params object.

## Prerequisite (NOT part of this plan)

Every test snippet below reaches fixture tasks as `tasks.<name>` / `atasks.<name>`. That
sweep — 16 test modules, ~167 references, `from tests.tasks import a, b` →
`from tests import tasks` + qualify — ships **on its own branch off `origin/main`,
merged first**, then `typed-absurd-params` rebases onto it. It is independent of #119
and would otherwise swamp this branch's pre-merge revdiff.

Two hazards for whoever does it: dotted-path **string literals** (`"tests.tasks.add"` in
assertions and `SCHEDULE` entries) are data, not references — they must stay
byte-identical; and `tests/core/test_scheduler.py:27`'s
`from tests.tasks import make_group as make_group_task` alias goes away
(`tasks.make_group` needs no rename). No fixture task is renamed — `capped`, `routed`,
`with_default_attempts` and friends read correctly once module-qualified.

## Global Constraints

These are the rules _specific to this work_. Everything in `CLAUDE.md` (imports, naming,
function-based pytest, no monkeypatching, no unit-testing internal helpers,
alphabetization, complete-message assertions, never amend) applies on top and is not
restated here.

- **Never add a ruff `noqa`/ignore** — restructure instead, or stop and ask.
- **No value validation on this surface.** `absurd_params` never checks that
  `max_attempts` is an int or that `retry_strategy["kind"]` is known. Wrong _data_ fails
  loudly on its own; only wrong-_door_ mistakes get curated messages.
- Narrow `# type: ignore[code]` is expected where a test deliberately passes something
  the checker rejects; `warn_unused_ignores` fails the build if the error stops
  occurring. Keep the comment short enough for ruff's 88-column limit (E501 exempts a
  line that is overlong _only_ because of a trailing pragma — verified).
- **mypy strict is the only type gate.** A pyright-only diagnostic on our own source is
  not a defect; judge a typed surface by what happens at a _user's_ call site.
- **The two gates**, both compose services up first
  (`docker compose up -d db db_pg_cron`):
  - `uvx --with tox-uv tox -e dev` — all three suites, dev env only.
  - `uv run pre-commit run --all-files` — owns ruff-check, ruff-format, mypy, prettier.
    Never invoke ruff or mypy directly.
  - Iterating on one file is still `uv run pytest <path> -v`.

## Runtime facts verified against this checkout (do not re-derive)

All of the following were confirmed by running code against this venv.

- `django.tasks.base.Task` runtime: `@dataclass(frozen=True, slots=True, kw_only=True)`,
  fields `priority, func, backend, queue_name, run_after, takes_context`.
  `Task.__post_init__` calls `self.get_backend().validate_task(self)`.
- `Task` is **not** subscriptable at runtime (`type 'Task' is not subscriptable`) but IS
  `Generic[_P, _R]` in django-stubs → needs the repo's TYPE_CHECKING conditional-base
  alias pattern (same as `ModelAdmin`/`Paginator` elsewhere here).
- The mechanism works: a frozen non-slots subclass of the frozen+slots base constructs;
  the setdefault fold is idempotent under repeated `replace()`/`using()` (a per-call
  value is never clobbered); the field survives `deepcopy` and `pickle`;
  `FrozenInstanceError`'s message is exactly `cannot assign to field 'absurd_params'`.
- **Ignore census — production needs exactly one.** `# type: ignore[misc]` on the
  `class AbsurdTask(TaskBase):` line (_not_ the decorator line): django-stubs declares
  `Task` non-frozen, so `Frozen dataclass cannot inherit from a non-frozen dataclass`.
  Pre-approved; do not file upstream. `task_class = AbsurdTask` is clean against the
  `type[Task]` stub. `bind` returning `Task[P, R]` needs **no** `t.cast` — `AbsurdTask`
  is statically `Task[Any, Any]`, which is Any-compatible.
- **Overload selection under mypy is as designed:** `absurd_params()` and
  `absurd_params(max_attempts=3)` → `AbsurdParams`; adding `headers`/`idempotency_key` →
  `PerCallParams`; `bind` preserves the ParamSpec.
- `assert isinstance(x, AbsurdTask)` narrows for both attribute reads and
  `dataclasses.replace(x, absurd_params=...)`. Without it mypy rejects the replace
  kwarg.
- `absurd_sdk` spawn options are exactly
  `max_attempts, retry_strategy, headers, queue, cancellation, idempotency_key`.
- `client.claim_tasks()` takes **no queue argument** — the queue is bound at client
  construction (`get_absurd_client()` → `"default"`), so a task enqueued to
  `other`/`reports` is invisible to a claim. Assert routing via `TaskResult.id`
  (`f"{queue_name}:{task_id}"`), never by claiming.
- An unset field never reaches the payload: a task spawned with only `max_attempts=3`
  claims back as `{'retry_strategy': None, ..., 'headers': None}`. Note this alone does
  **not** prove omission (the SDK drops `None` values anyway) — pin omission at the
  params dict, see Task 2's `test_unset_fields_never_enter_the_params`.
- Adding an `immediate` backend to `tests/settings.py` is invisible to django-absurd's
  checks (every one filters through `get_absurd_backends()`, an isinstance test, and
  `BaseTaskBackend.check()` returns `[]`). `ImmediateBackend` defaults to
  `queues={"default"}`, so the cross-backend `.using()` paths validate. Caveat:
  `tests/multidb/settings.py` **replaces** `TASKS` wholesale, so it does not inherit the
  alias — harmless, only `tests/core` uses it.

---

### Task 1: `AbsurdTask` — params resolve from one field

**Files:**

- Create: `django_absurd/tasks.py`
- Create: `tests/core/test_absurd_task.py`
- Modify: `django_absurd/params.py` (`SpawnKwargs` moves out; the decorator writes
  `func.absurd_params`)
- Modify: `django_absurd/backends.py` (`task_class`; field read; **remove** the
  `django_absurd.params` import)
- Modify: `django_absurd/pg_cron/reconcile.py:12,48-49`
- Modify: `tests/settings.py` (add a non-Absurd `immediate` backend)
- Modify: `tests/core/test_enqueue.py` (two cases)
- Modify: `tests/core/test_params.py` (delete one test — see notes)

**Interfaces:**

- Consumes: nothing.
- Produces:
  - `django_absurd.tasks.SpawnKwargs` — `t.TypedDict, total=False`: `max_attempts: int`,
    `retry_strategy: RetryStrategy`, `cancellation: CancellationPolicy`,
    `headers: JsonObject`, `idempotency_key: str`. Moved verbatim from `params.py`.
  - `django_absurd.tasks.build_merged_spawn_options(defaults: SpawnKwargs | None, per_call: SpawnKwargs | None) -> SpawnKwargs`
    — later layer wins per key; **deep-copies** each layer's nested values.
  - `django_absurd.tasks.AbsurdTask` — frozen, kw_only dataclass subclass of `Task`, one
    added field `absurd_params: SpawnKwargs | None = None`.
  - `AbsurdBackend.task_class = AbsurdTask`.
  - Invariant later tasks rely on: for a task on the Absurd backend all resolved params
    live in `AbsurdTask.absurd_params`; `func.absurd_params` holds decorator defaults
    only, as a plain `SpawnKwargs`.

**Notes for the implementer:**

- `tasks.py` must stay a **leaf** — it imports `absurd_sdk` types and
  `django.tasks.base.Task`, nothing from `django_absurd`. Task 2 depends on this to
  import both `tasks` and `backends` from `params` without a cycle.
- The subclass needs the conditional-base alias, because `Task[...]` explodes at
  runtime:

  ```python
  if t.TYPE_CHECKING:
      TaskBase = Task[t.Any, t.Any]
  else:
      TaskBase = Task
  ```

- `__post_init__` reads `getattr(self.func, "absurd_params", None)` and, when present,
  writes `build_merged_spawn_options(<func attr>, self.absurd_params)` back with
  `object.__setattr__`, then calls `super().__post_init__()` so `validate_task` still
  runs. Defaults-first ordering is what makes the fold idempotent under `replace()`.
- **`backends.py` must end this task importing nothing from `params.py`.** After the
  merge helper moves to `tasks.py`, nothing there needs `AbsurdDefaultParams` — both
  remaining reads are untyped (`getattr`, `pop`). Leaving the import in place turns Task
  2 into a hard circular import (`__init__` → `params` → `backends` → `params`,
  partially initialized). Do not "use" it in an annotation to keep it alive.
- `enqueue` reads params as: the field when `isinstance(task, AbsurdTask)`, else
  `getattr(task.func, "absurd_params", None)`. The `else` is load-bearing — a task
  defined on another backend and routed in with `.using(backend=...)` keeps
  `task_class = Task`. Note a task defined on THIS backend stays an `AbsurdTask` through
  any `.using()`, scheduler routing included, so only a foreign-backend task reaches the
  `else`. It gets its own test below.
- `merged` must be a fresh dict — `setdefault("max_attempts", …)` on the task's own
  field would mutate stored params. Route through `build_merged_spawn_options`, which
  returns a new dict, rather than mutating what you read.
- The legacy `kwargs.pop("absurd_spawn_params", None)` stays, layered **on top** of the
  field. Task 3 deletes it.
- `reconcile.resolve_spawn_options` reads `getattr(task.func, "absurd_params", None)` —
  and **drops its `.to_kwargs()` call**, since that attribute is now a plain dict. Its
  own `setdefault` floor stays. Both `setdefault("max_attempts", …)` calls (backend +
  reconcile) are the infinite-retry guard: an omitted value lands as NULL and
  `absurd.fail_run` then retries forever. Never "clean up".
- `absurd_default_params` keeps building `AbsurdDefaultParams` internally (that
  dataclass is what rejects a per-invocation keyword today) and writes
  `params.to_kwargs()` to the attribute, keeping its narrow
  `# type: ignore[attr-defined]  # dynamic attribute on decorated callable`.
- **Delete `tests/core/test_params.py::test_decorator_attaches_default_to_task_func`**
  in this task. It asserts the old `func.absurd_default_params` attribute, which stops
  existing here; the suite cannot go green otherwise. The spec already slates it for
  deletion ("asserts internals"), and the behavioral
  `test_max_attempts_uses_decorator_default` covers the same ground.
- Record the spec's two accepted `retry_strategy` consequences as a comment at that
  field's declaration in `SpawnKwargs`: an unrecognized `kind` is validated nowhere
  (`fail_run` coalesces to `'none'`, delay 0), and an omitted `kind` raises
  `KeyError: 'kind'` inside `absurd_sdk._serialize_retry_strategy` even though
  `RetryStrategy` is `total=False`.

- [ ] **Step 1: Add the test-only backend**

`tests/settings.py` — a second, non-Absurd alias, needed by this task's `else`-branch
test and by Task 2's off-backend tests:

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default", "other", "reports"],
    },
    "immediate": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"},
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/core/test_absurd_task.py`. It drives real decorated tasks from
`tests/tasks.py` — nothing hand-sets the function attribute:

```python
import dataclasses

import pytest
from django.tasks import Task

from django_absurd.tasks import AbsurdTask
from tests import tasks

FULLY_SPECCED_PARAMS = {
    "max_attempts": 9,
    "retry_strategy": {"kind": "fixed", "base_seconds": 5},
    "cancellation": {"max_duration": 45, "max_delay": 3},
}


def test_absurd_backend_builds_absurd_tasks() -> None:
    assert isinstance(tasks.add, AbsurdTask)
    assert isinstance(tasks.add, Task)
    assert tasks.add.absurd_params is None


def test_decorator_defaults_fold_into_the_field() -> None:
    assert isinstance(tasks.fully_specced, AbsurdTask)
    assert tasks.fully_specced.absurd_params == FULLY_SPECCED_PARAMS


def test_using_preserves_the_field() -> None:
    routed = tasks.fully_specced.using(queue_name="other")
    assert isinstance(routed, AbsurdTask)
    assert routed.absurd_params == FULLY_SPECCED_PARAMS


def test_a_per_call_value_wins_per_field_over_the_defaults() -> None:
    assert isinstance(tasks.fully_specced, AbsurdTask)
    bound = dataclasses.replace(tasks.fully_specced, absurd_params={"max_attempts": 1})
    assert isinstance(bound, AbsurdTask)
    assert bound.absurd_params == {**FULLY_SPECCED_PARAMS, "max_attempts": 1}


def test_using_does_not_clobber_a_per_call_value() -> None:
    assert isinstance(tasks.fully_specced, AbsurdTask)
    bound = dataclasses.replace(tasks.fully_specced, absurd_params={"max_attempts": 1})
    replaced = bound.using(queue_name="other")
    assert isinstance(replaced, AbsurdTask)
    assert replaced.absurd_params == {**FULLY_SPECCED_PARAMS, "max_attempts": 1}


def test_the_field_is_read_only() -> None:
    assert isinstance(tasks.add, AbsurdTask)
    with pytest.raises(
        dataclasses.FrozenInstanceError,
        match="cannot assign to field 'absurd_params'",
    ):
        tasks.add.absurd_params = {"max_attempts": 1}
```

Append to `tests/core/test_enqueue.py` — the two paths through the new resolution. Claim
inline, no helper indirection. Three imports are new to that file: `task` from
`django.tasks`, `absurd_default_params` from `django_absurd.params`, and `AbsurdTask`
from `django_absurd.tasks`:

```python
@task(backend="immediate")
@absurd_default_params(max_attempts=4)
def add_on_immediate_backend(a: int, b: int) -> int:
    return a + b


def test_decorator_default_survives_a_replace() -> None:
    # .using() is dataclasses.replace(), which re-runs __post_init__ and re-folds.
    call_command("absurd_sync_queues")
    tasks.with_default_attempts.using(priority=0).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 7


def test_a_plain_task_routed_in_keeps_its_decorator_default() -> None:
    # ImmediateBackend keeps task_class = Task and .using() preserves the class, so
    # this reaches enqueue as a bare Task — the getattr branch, not the field branch.
    call_command("absurd_sync_queues")
    routed = add_on_immediate_backend.using(backend="default")
    assert not isinstance(routed, AbsurdTask)
    routed.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 4
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/core/test_absurd_task.py tests/core/test_enqueue.py -v`

Expected: `test_absurd_task.py` fails collection —
`ModuleNotFoundError: No module named 'django_absurd.tasks'`. That is this task's RED.

**Be honest about the other two.** `test_decorator_default_survives_a_replace` and
`test_a_plain_task_routed_in_keeps_its_decorator_default` **pass today** via the legacy
`func.absurd_default_params` path — they are regression guards and branch coverage, not
RED tests. Do not expect them to fail and do not "fix" anything when they don't. The
genuine mid-task RED appears between Step 5 and Step 6: once the decorator writes the
new attribute but `enqueue` still reads the old one,
`test_max_attempts_uses_decorator_default` fails `assert 5 == 7`.

- [ ] **Step 4: Create `django_absurd/tasks.py`**

Module docstring: the Absurd-backend `Task` subclass and the spawn-option primitives it
carries. Contents in order: `SpawnKwargs` (moved from `params.py`, with the
`retry_strategy` comment), the TYPE_CHECKING `TaskBase` alias, `AbsurdTask`, then
`build_merged_spawn_options` below it. Explain in `AbsurdTask`'s docstring why params
live in a field rather than `__slots__` (`using()` is `replace()`), and why the class
carries the `# type: ignore[misc]`.

- [ ] **Step 5: Rename what the decorator writes**

`params.py`: delete `SpawnKwargs`, import it from `django_absurd.tasks`, and have
`absurd_default_params` write `params.to_kwargs()` to `func.absurd_params`. Delete
`tests/core/test_params.py::test_decorator_attaches_default_to_task_func` in the same
step — it asserts the attribute that just stopped existing.

- [ ] **Step 6: Rewire `backends.py`**

Delete `build_merged_spawn_options` from `backends.py`; import it and `AbsurdTask` from
`django_absurd.tasks`; **delete the `from django_absurd.params import ...` line
entirely**. Add `task_class = AbsurdTask` alongside the other class attributes. In
`enqueue`, replace the `getattr(task.func, "absurd_default_params", None)` read with the
isinstance-narrowed field read plus its `getattr` else, keeping the legacy kwarg pop as
the top layer. No `getattr` on the `AbsurdTask` branch, no `t.Any`, no ignore.

- [ ] **Step 7: Rewire `pg_cron/reconcile.py`**

`resolve_spawn_options` imports the merge helper from `django_absurd.tasks`, reads
`getattr(task.func, "absurd_params", None)`, and passes it straight through — **no
`.to_kwargs()`**. Its `setdefault` floor stays.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
uv run pytest tests/core/test_absurd_task.py -v
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
```

Expected: all PASS, including `test_max_attempts_uses_decorator_default`,
`test_per_call_max_attempts_overrides_decorator_and_backend`, and
`tests/pg_cron/test_pg_cron_options.py`'s "7, not 5" case. Exactly one new production
`# type: ignore[misc]`.

- [ ] **Step 9: Commit**

```bash
git add django_absurd/tasks.py django_absurd/params.py django_absurd/backends.py \
        django_absurd/pg_cron/reconcile.py tests/settings.py \
        tests/core/test_absurd_task.py tests/core/test_enqueue.py \
        tests/core/test_params.py
git commit -m "feat: resolve spawn params from an AbsurdTask dataclass field"
```

---

### Task 2: The `absurd_params` public API

**Files:**

- Modify: `django_absurd/params.py` (add the new surface; old symbols stay until Task 3)
- Modify: `django_absurd/__init__.py` (export `absurd_params`)
- Create: `tests/core/test_absurd_params.py` (DB-backed behavior)
- Create: `tests/core/test_absurd_params_guards.py` (pure runtime errors, no DB marks)

**Interfaces:**

- Consumes: `AbsurdTask`, `SpawnKwargs`, `build_merged_spawn_options` from Task 1.
- Produces:

  ```python
  P = t.ParamSpec("P")
  R = t.TypeVar("R")

  @dataclasses.dataclass(frozen=True)
  class ParamsBase:
      kwargs: SpawnKwargs
      def bind(self, target: "Task[P, R]") -> "Task[P, R]": ...

  class AbsurdParams(ParamsBase):
      def __call__(self, target: t.Callable[P, R]) -> t.Callable[P, R]: ...

  class PerCallParams(ParamsBase):          # sibling, NOT a subclass
      def __call__(self, target: t.Never) -> t.NoReturn: ...

  @t.overload
  def absurd_params(
      *,
      max_attempts: int = ...,
      retry_strategy: RetryStrategy = ...,
      cancellation: CancellationPolicy = ...,
  ) -> AbsurdParams: ...
  @t.overload
  def absurd_params(
      *,
      max_attempts: int = ...,
      retry_strategy: RetryStrategy = ...,
      cancellation: CancellationPolicy = ...,
      headers: JsonObject = ...,
      idempotency_key: str = ...,
  ) -> PerCallParams: ...
  ```

**Notes for the implementer:**

- **Import direction.** `params.py` may now import `django_absurd.tasks` AND
  `django_absurd.backends` at module level — Task 1 removed the reverse edge, so there
  is no cycle and no function-local import (hence no `noqa`). Verified:
  `django_absurd.backends` imports cleanly with no settings configured, and importing
  before `django.setup()` then calling setup also succeeds, so the new `__init__.py`
  export is safe for app loading.
- The two classes are **siblings** over `ParamsBase` on purpose. If either subclassed
  the other, mypy would reject the `__call__` override: `NoReturn` is the bottom type,
  so no subclass can widen it.
- Implementation signature spells out all five keyword-only params (defaulting to
  `NOT_SET`) plus `**unsupported: object`, so `inspect.signature` stays informative and
  unknown keys reach the curated message instead of Python's bare `TypeError`. Return
  `PerCallParams` when `headers` or `idempotency_key` is set, else `AbsurdParams`.
  `absurd_params()` with no fields is legal and yields empty `kwargs`.
- `bind` order of operations: reject a non-`Task` target; return the target unchanged
  (with a once-per-`module_path` `WARNING`) when
  `isinstance(target.get_backend(), AbsurdBackend)` is false; otherwise rebuild via
  `{f.name: getattr(target, f.name) for f in dataclasses.fields(Task)}` plus
  `absurd_params=build_merged_spawn_options(<target's existing field or None>, self.kwargs)`.
  Rebuilding from `dataclasses.fields(Task)` is why `bind` does not assume the target is
  already an `AbsurdTask`. No `t.cast` needed on the return.
- Logger: `logging.getLogger("django_absurd")`, past-tense message, deduped through a
  module-level `WARNED_TASK_PATHS: set[str]`.
- `AbsurdParams.__call__` raises when handed a `Task` (params applied above `@task`);
  otherwise it merges into `func.absurd_params` with setdefault semantics (so stacked
  decorators merge, later value winning) and returns the function unchanged.
- No double-apply or double-bind guards. Merging mirrors `Task.using()`; not documented.
- **Curated messages: the tests below are the binding source.** Each message's complete
  text is asserted there; do not maintain a second copy in prose. Two rules the tests
  don't pin: the unknown-keyword message names the **first** unsupported keyword in call
  order, and the definition-site message lists the per-invocation fields present as
  `", ".join(sorted(...))` with the snippet using the first of them. `queue_name` shares
  the routing message with `queue` — a spec deviation (the spec names only `queue`, on
  the basis that it is a real Absurd spawn option), justified because a user reaching
  for Django's own spelling is at exactly the wrong door the curated category exists
  for.
- The target's display name in the definition-site message is
  `getattr(target, "name", getattr(target, "__name__", repr(target)))` — inline it at
  the raise site; a `Task` exposes `.name`, a function `__name__`.

- [ ] **Step 1: Write the failing tests**

Two files. The guards need no database, so they stay out of the module that pays for
`transaction=True` + `_isolate_queues`' schema hard-drop.

`tests/core/test_absurd_params.py`:

```python
import asyncio
import logging
import typing as t

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connections
from django.tasks import Task, task

from django_absurd import absurd_params
from django_absurd.connection import register_jsonb_loader
from django_absurd.queues import get_absurd_client
from django_absurd.tasks import AbsurdTask
from tests import tasks

if t.TYPE_CHECKING:
    from absurd_sdk import JsonObject, RetryStrategy

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


@task(backend="immediate")
def make_group_on_immediate_backend(name: str) -> str:
    Group.objects.create(name=name)
    return name


@task(backend="immediate")
def multiply_on_immediate_backend(a: int, b: int) -> int:
    return a * b


def test_bind_returns_a_real_task() -> None:
    bound = absurd_params(max_attempts=3).bind(tasks.add)
    assert isinstance(bound, Task)
    assert isinstance(bound, AbsurdTask)
    assert bound.func is tasks.add.func
    assert isinstance(tasks.add, AbsurdTask)
    assert tasks.add.absurd_params is None


def test_an_empty_call_is_legal_and_adds_nothing() -> None:
    bound = absurd_params().bind(tasks.with_default_attempts)
    assert isinstance(bound, AbsurdTask)
    assert bound.absurd_params == {"max_attempts": 7}


def test_unset_fields_never_enter_the_params() -> None:
    # Exact-dict equality is what pins omission: an unset field must not become a
    # key at all. The claimed payload can't prove this — the SDK drops None values.
    bound = absurd_params(max_attempts=3).bind(tasks.add)
    assert isinstance(bound, AbsurdTask)
    assert bound.absurd_params == {"max_attempts": 3}


def test_bound_task_aenqueues() -> None:
    call_command("absurd_sync_queues")
    asyncio.run(absurd_params(max_attempts=3).bind(tasks.add).aenqueue(1, 2))
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 3


def test_a_later_plain_enqueue_still_sees_the_default() -> None:
    call_command("absurd_sync_queues")
    absurd_params(max_attempts=9).bind(tasks.with_default_attempts).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    client = get_absurd_client()
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 9
    tasks.with_default_attempts.enqueue(1, 2)
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 7


def test_headers_are_copied_away_from_the_caller() -> None:
    # Mutate between bind and enqueue: after enqueue the payload is already in
    # Postgres and the assertion would hold even with every deep-copy deleted.
    call_command("absurd_sync_queues")
    headers: JsonObject = {"trace": "abc"}
    bound = absurd_params(headers=headers).bind(tasks.add)
    headers["trace"] = "mutated-before-enqueue"
    bound.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["headers"] == {"trace": "abc"}


def test_params_survive_using_in_both_orderings() -> None:
    call_command("absurd_sync_queues")
    register_jsonb_loader(connections["default"].connection)
    client = get_absurd_client()
    absurd_params(max_attempts=9).bind(
        tasks.with_default_attempts.using(priority=0)
    ).enqueue(1, 2)
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 9
    absurd_params(max_attempts=9).bind(tasks.with_default_attempts).using(
        priority=0
    ).enqueue(1, 2)
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 9


def test_binding_before_routing_still_routes() -> None:
    call_command("absurd_sync_queues")
    result = (
        absurd_params(max_attempts=9)
        .bind(tasks.with_default_attempts)
        .using(queue_name="other")
        .enqueue(1, 2)
    )
    assert result.id.startswith("other:")


def test_a_task_from_another_backend_binds_and_spawns() -> None:
    call_command("absurd_sync_queues")
    routed = multiply_on_immediate_backend.using(backend="default")
    assert not isinstance(routed, AbsurdTask)  # kept task_class = Task
    absurd_params(max_attempts=9).bind(routed).enqueue(3, 4)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 9


def test_repeated_binds_merge_with_the_later_value_winning() -> None:
    call_command("absurd_sync_queues")
    strategy: RetryStrategy = {"kind": "none"}
    once = absurd_params(max_attempts=9, retry_strategy=strategy).bind(tasks.add)
    absurd_params(max_attempts=4).bind(once).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)[0]
    assert claimed["max_attempts"] == 4
    assert claimed["retry_strategy"] == strategy


def test_binding_off_backend_is_a_deduped_no_op(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        first = absurd_params(max_attempts=9).bind(make_group_on_immediate_backend)
        second = absurd_params(max_attempts=9).bind(make_group_on_immediate_backend)
    assert first is make_group_on_immediate_backend
    assert second is make_group_on_immediate_backend
    assert caplog.messages == [
        "absurd_params ignored: tests.core.test_absurd_params."
        "make_group_on_immediate_backend is on task backend 'immediate', "
        "which is not an Absurd backend"
    ]
    first.enqueue("off-backend-ran")
    assert Group.objects.filter(name="off-backend-ran").exists()


def test_an_absurd_task_routed_off_backend_is_also_a_no_op() -> None:
    routed = tasks.with_default_attempts.using(backend="immediate")
    assert absurd_params(max_attempts=9).bind(routed) is routed
```

`tests/core/test_absurd_params_guards.py` — one test per guard row, asserting the
complete message:

```python
import typing as t

import pytest

from django_absurd import absurd_params
from tests import tasks

if t.TYPE_CHECKING:
    from absurd_sdk import RetryStrategy


def test_unknown_keyword_is_rejected() -> None:
    expected = (
        "'max_attemps' is an invalid argument for absurd_params. Valid arguments: "
        "max_attempts, retry_strategy, cancellation, headers, idempotency_key."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(max_attemps=3)  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_queue_is_rejected_with_a_pointer_to_using() -> None:
    expected = (
        "'queue' cannot be set through absurd_params — queue routing belongs to "
        'Django:\n\n    send_report.using(queue_name="reports")'
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(queue="reports")  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_queue_name_gets_the_same_pointer() -> None:
    expected = (
        "'queue_name' cannot be set through absurd_params — queue routing belongs "
        'to Django:\n\n    send_report.using(queue_name="reports")'
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(queue_name="reports")  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_run_after_is_rejected_with_the_defer_pointer() -> None:
    expected = (
        "'run_after' cannot be set through absurd_params, and deferred enqueue is "
        "not supported by this backend (supports_defer = False). See "
        "https://github.com/lincolnloop/django-absurd/issues/116."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(run_after=None)  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_per_invocation_field_cannot_decorate() -> None:
    expected = (
        "idempotency_key can only be set per invocation, not on a task "
        "definition. Bind it at enqueue time instead:\n\n"
        "    absurd_params(idempotency_key=...).bind(send_report).enqueue(...)"
    )

    def send_report(user_id: int) -> None:
        return None

    with pytest.raises(TypeError) as excinfo:
        absurd_params(idempotency_key="k")(send_report)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_params_above_task_are_rejected() -> None:
    expected = (
        "apply @absurd_params below @task, not above it:\n\n"
        "    @task\n    @absurd_params(max_attempts=3)\n"
        "    def send_report(user_id: int) -> None: ..."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(max_attempts=3)(tasks.add)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_bind_rejects_a_non_task() -> None:
    expected = (
        "absurd_params(...).bind() takes a Task, got int. To set defaults on a "
        "task definition, apply absurd_params(...) as a decorator below @task."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(max_attempts=3).bind(3)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_stacked_decorators_merge_the_same_way() -> None:
    strategy: RetryStrategy = {"kind": "none"}

    @absurd_params(max_attempts=4)
    @absurd_params(max_attempts=9, retry_strategy=strategy)
    def stacked(a: int, b: int) -> int:
        return a + b

    attached: t.Any = stacked
    assert attached.absurd_params == {"max_attempts": 4, "retry_strategy": strategy}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
`uv run pytest tests/core/test_absurd_params.py tests/core/test_absurd_params_guards.py -v`
Expected: collection error in both —
`ImportError: cannot import name 'absurd_params' from 'django_absurd'`.

- [ ] **Step 3: Build the params surface**

Add to `django_absurd/params.py`, in this order: the `P`/`R` type variables,
`ParamsBase` (with `bind`), `AbsurdParams`, `PerCallParams`, the two `absurd_params`
overloads and its implementation, then the message builders and `WARNED_TASK_PATHS`
below them. Leave `AbsurdDefaultParams`, `AbsurdSpawnParams`, `AbsurdFieldValue`, and
`absurd_default_params` in place — Task 3 removes them.

- [ ] **Step 4: Export from the package root**

`django_absurd/__init__.py`: `from django_absurd.params import absurd_params`, added to
`__all__` (ruff `RUF022` enforces the sort).

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/core/test_absurd_params.py tests/core/test_absurd_params_guards.py -v
uv run pytest tests/core -n4          # dedup is process-wide; prove it under xdist
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
```

Expected: all PASS. mypy passing with every `# type: ignore[...]` above still _used_ is
the static half of the contract — `warn_unused_ignores` turns a silently-weakened
overload or `Never` annotation into a build failure. Watch for ruff `TC002` on any
`absurd_sdk` import that ends up annotation-only; both test files above already put
those under `if t.TYPE_CHECKING`.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/params.py django_absurd/__init__.py \
        tests/core/test_absurd_params.py tests/core/test_absurd_params_guards.py
git commit -m "feat: add absurd_params factory with decorator and bind call sites"
```

---

### Task 3: Migrate every call site and delete the old API

**Files:**

- Modify: `django_absurd/params.py` (delete `AbsurdDefaultParams`, `AbsurdSpawnParams`,
  `AbsurdFieldValue`, `absurd_default_params`)
- Modify: `django_absurd/backends.py` (delete the `kwargs.pop("absurd_spawn_params", …)`
  layer)
- Modify: `django_absurd/scheduler.py:17,78`
- Modify: `django_absurd/AGENTS.md` (7 refs — see notes; it is **not** under `docs/`)
- Modify: `tests/tasks.py` (5 decorator lines + import)
- Modify: `tests/core/test_enqueue.py`, `test_durable.py`, `test_results.py`,
  `test_async_worker.py`, `test_orm_models.py`, `test_pytest_plugin.py`,
  `test_admin/utils.py`
- Modify: `tests/pg_cron/test_pg_cron_options.py:20` (comment only)
- Delete: `tests/core/test_params.py`

**Interfaces:**

- Consumes: the full `absurd_params` surface from Task 2.
- Produces: no reference to `absurd_default_params`, `AbsurdSpawnParams`,
  `AbsurdDefaultParams`, `AbsurdFieldValue`, or `absurd_spawn_params` anywhere in the
  repo except `docs/` (Task 4) and this plan plus the spec.

**Notes for the implementer:**

- Mechanical shapes:
  - `@absurd_default_params(max_attempts=7)` → `@absurd_params(max_attempts=7)`
  - `x.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(max_attempts=9))` →
    `absurd_params(max_attempts=9).bind(x).enqueue(1, 2)`
  - `from django_absurd.params import ...` → `from django_absurd import absurd_params`
- The `# type: ignore[call-arg]` comments must **go away** — that is the point of the
  change, and `warn_unused_ignores` fails the build if one is left. There are **6** in
  `test_enqueue.py` (lines 141, 149, 163, 171, 174, 188) and **1** in `test_results.py`.
- Task 1 added `add_on_immediate_backend` to `test_enqueue.py` carrying
  `@absurd_default_params(max_attempts=4)` — respell it too.
- **Delete three now-duplicated tests** in `test_enqueue.py`, same delete-not-duplicate
  discipline the spec applies to `test_params.py`:
  `test_per_call_max_attempts_overrides_decorator_and_backend` (subsumed by Task 2's
  `test_a_later_plain_enqueue_still_sees_the_default`, which also covers the reversion
  the old test misses), `test_headers_reach_spawn` and `test_idempotency_key_dedups` —
  but only after confirming Task 2's coverage; `test_retry_strategy_reaches_spawn` and
  `test_spawn_params_not_passed_to_task_func` have no twin and are migrated, the latter
  explicitly kept as a regression guard per the spec.
- While rewriting `test_enqueue.py`'s call sites anyway, delete its local `claim_one`
  helper and inline the two claim lines at each of its **11** uses. No indirect
  abstraction over a two-line claim.
- `tests/core/test_params.py` is deleted outright. After Task 1 removed
  `test_decorator_attaches_default_to_task_func`, what remains is:
  `test_to_kwargs_emits_only_set_fields` and
  `test_spawnparams_carries_per_invocation_fields` (subjects deleted; covered by Task
  2's `test_unset_fields_never_enter_the_params` and the migrated headers/idempotency
  tests), and `test_decorator_rejects_per_invocation_kwarg` /
  `test_decorator_above_task_raises` (covered by Task 2's
  `test_per_invocation_field_cannot_decorate` and
  `test_params_above_task_are_rejected`). Nothing is left worth keeping. Confirm with
  `ag "good_default" tests/` that its module-level task is unreferenced.
- **`django_absurd/AGENTS.md` is migrated here, not in Task 4.** It carries 7
  old-spelling refs (lines 175-193, 505) and lives outside `docs/`, so the Step 4 sweep
  would fail otherwise. Task 4 still owns its _prose_ rewrite; this task only respells
  the identifiers.

- [ ] **Step 1: Migrate the tests and delete `test_params.py`**

Run `ag -l "absurd_default_params|absurd_spawn_params|AbsurdSpawnParams"` and work the
list. Apply the mechanical shapes; remove every now-unused ignore; apply the three
deletions above.

- [ ] **Step 2: Migrate `scheduler.py` and `AGENTS.md`**

`spawn_scheduled` becomes
`absurd_params(idempotency_key=derive_idempotency_key(schedule, slot)).bind(task).enqueue(*schedule.args, **schedule.kwargs)`;
swap the `AbsurdSpawnParams` import for
`from django_absurd.params import absurd_params`. Respell the 7 `AGENTS.md` identifier
references.

No RED step here: the legacy path stays functional until Step 3, so the suite is green
throughout Steps 1–2. Step 3 is what forces the migration to be complete.

- [ ] **Step 3: Delete the old API**

Remove `AbsurdDefaultParams`, `AbsurdSpawnParams`, `AbsurdFieldValue`, and
`absurd_default_params` from `params.py`, and the
`kwargs.pop("absurd_spawn_params", None)` layer from `AbsurdBackend.enqueue`. Update
`params.py`'s module docstring — it no longer describes dataclasses.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uvx --with tox-uv tox -e dev
uv run pre-commit run --all-files
ag "absurd_default_params|absurd_spawn_params|AbsurdSpawnParams|AbsurdDefaultParams" \
   --ignore-dir=docs --ignore-dir=.git
```

Expected: suites and gates PASS; the `ag` sweep returns nothing (`AGENTS.md` was handled
in Step 2).

- [ ] **Step 5: Verify patch coverage**

Run: `uv run pytest tests/core --cov=django_absurd --cov-branch` Expected: every line
and branch added in Tasks 1–3 is covered, including `enqueue`'s non-`AbsurdTask` `else`
(Task 1's `test_a_plain_task_routed_in_keeps_its_decorator_default`). Cover a gap
through a real entrypoint or delete the unreachable defense — no pragma.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor!: replace absurd_default_params and the spawn-params kwarg"
```

---

### Task 4: Documentation

**Files:**

- Modify: `docs/web/tasks.md:34-86` ("Retries & spawn options")
- Modify: `docs/web/cron-jobs.md:244`
- Modify: `django_absurd/AGENTS.md:172-199, 505` (prose; identifiers already respelled)
- Modify: `django_absurd/__init__.py` module docstring

**Interfaces:**

- Consumes: the shipped API from Tasks 1–3.
- Produces: no doc references the deleted spellings.

**Notes for the implementer:**

Invoke the `sync-docs` skill and follow it; the list below is what to verify afterwards,
not a substitute for the skill.

- `docs/web/tasks.md`: respell both examples against the spec's "Public surface"
  snippet. Add two notes: Django's own options (routing, `.using(queue_name=...)`) stay
  on `.using()` and order is free; binding on a non-Absurd backend is a no-op that warns
  once. The field table stands — its "default + per-call" / "per-call only" column is
  now signature-enforced by the overload pair.
- Link the decorator fields to Absurd's
  [task definition](https://earendil-works.github.io/absurd/) defaults
  (`default_max_attempts`, `default_cancellation`) **without claiming field parity**:
  `register_task` takes no `retry_strategy`, so that one is ours, applied at spawn.
- `AGENTS.md`: state that `bind` returns an ordinary `Task` (`isinstance` holds;
  `aenqueue`/`call`/`get_result`/`using` all work through it) and that the symbol is
  exported from the package root.
- `__init__.py` docstring already says "enqueue params/decorators" — extend it to name
  the exported `absurd_params`.
- `examples/`: `ag "absurd_default_params|absurd_spawn_params" examples/` returns
  nothing today. Re-check; if a hit appears, import from the package root.
- Hyperlink every cross-reference to another doc page/section.

- [ ] **Step 1: Invoke `sync-docs` and apply the edits**

- [ ] **Step 2: Verify no stale spellings remain**

```bash
ag "absurd_default_params|absurd_spawn_params|AbsurdSpawnParams|AbsurdDefaultParams" \
   --ignore-dir=.git
```

Expected: hits only in `docs/specs/2026-07-27-typed-absurd-params.md`, this plan, and
`docs/HISTORY.md` if it already mentions them.

- [ ] **Step 3: Verify the docs examples are real**

Run: `uv run pytest tests/core/test_packaging.py -v` Expected: PASS (it checks the
shipped `AGENTS.md`). Then read each edited snippet against
`tests/core/test_absurd_params.py` and confirm the spellings match exactly.

- [ ] **Step 4: Commit**

```bash
git add docs/ django_absurd/AGENTS.md django_absurd/__init__.py
git commit -m "docs: respell spawn params as absurd_params"
```

---

## Follow-ups (do NOT do in this plan)

- Reserve a header namespace when
  [#116](https://github.com/lincolnloop/django-absurd/issues/116) starts carrying
  `run_after` in headers.
- Report upstream to Absurd: `RetryStrategy["kind"]` should be `Required`, or
  `_serialize_retry_strategy` should default it to `"none"` the way `fail_run` already
  does. A type-legal value currently raises `KeyError`.

## Finishing

Local branch `typed-absurd-params`, human-in-the-loop. Do NOT push or open a PR. After
Task 4: `git fetch origin`, then `revdiff` against `origin/main`, then an adversarial
review on the best available model, then wait for Marc.
