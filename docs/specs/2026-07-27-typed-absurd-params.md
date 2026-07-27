# Typed Absurd params: one `absurd_params`, two sites

Status: designed, not built. Supersedes `@absurd_default_params` +
`absurd_spawn_params=` kwarg.

## Problem

Per-invocation Absurd params ride a magic kwarg on `.enqueue()`. Two defects, both
measured (mypy 2.3.0 strict, django-stubs 6.0.7):

1. **Untypeable.** django-stubs types
   `Task.enqueue(self, *args: _P.args, **kwargs: _P.kwargs)`
   (`django-stubs/tasks/base.pyi:50`), where `_P` is the task function's own ParamSpec.
   No kwarg outside the task signature can type-check:

   ```
   error: Unexpected keyword argument "absurd_spawn_params" for "enqueue" of "Task"  [call-arg]
   ```

   Every strict user needs a `# type: ignore[call-arg]` per call. Our own suite already
   carries one (`tests/core/test_results.py:78`).

2. **Squats the task's kwargs namespace.** `backends.py:106` pops `absurd_spawn_params`
   before spawn. Task with a param of that name silently loses its argument until the
   worker runs. Same hazard for `**kwargs` passthrough (see `scheduler.py:78`, which
   splats `**schedule.kwargs` alongside the magic kwarg).

Third defect, found while measuring: **decorator not actually typed today.**
`params.py:64` is `**kwargs: AbsurdFieldValue` — a union already containing `str`, no
field-name checking. Both of these pass mypy clean:

```python
@absurd_default_params(max_attempts="not-an-int")   # no error
@absurd_default_params(max_attemps=3)               # no error -> TypeError at import
```

So "typed via the decorator" was never true.

## Locked decisions

| Question                   | Decision                                                                                                                                            |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Settings layer             | **Unchanged.** `DEFAULT_MAX_ATTEMPTS` stays the only settings-level param (`max_attempts` only). No new `OPTIONS` key.                              |
| Decorator field set        | **Unchanged** from today (`max_attempts`, `retry_strategy`, `cancellation`). Rename only.                                                           |
| Per-invocation field set   | Unchanged: decorator fields + `headers` + `idempotency_key`.                                                                                        |
| Old spellings              | **Hard break.** Delete `@absurd_default_params`, the `absurd_spawn_params=` kwarg, both param dataclasses. Pre-1.0; the kwarg never type-checked.   |
| Return value               | **Real `Task`, 1:1.** `isinstance(Task)` holds; `aenqueue`/`call`/`get_result`/`using` inherited. Original task untouched.                          |
| Bind spelling              | **`.bind(task)` method** for the enqueue site. `__call__` exists only for the decorator site (function target), so the two sites never overlap.     |
| Per-site separation        | **Static**, via an overload pair on `absurd_params`. Runtime guard kept as backstop for untyped callers.                                            |
| Double apply (decorator)   | **Raise.**                                                                                                                                          |
| Double bind (enqueue site) | **Raise.**                                                                                                                                          |
| Non-Absurd backend         | **No-op**, `WARNING` once per task on the `django_absurd` logger.                                                                                   |
| Typing tests               | Negative cases pinned with narrow `# type: ignore[...]` under strict `warn_unused_ignores`. Ignores authorized **for the typing-test module only**. |

## Public surface

One name, two sites. Decorator site keeps today's placement (below `@task`).

```python
from django.tasks import task
from django_absurd.params import absurd_params


@task
@absurd_params(max_attempts=3)
def send_report(user_id: int) -> None: ...


# per-invocation
absurd_params(idempotency_key=f"report:{42}").bind(send_report).enqueue(42)

# composes with Django's own channel — apply params last
absurd_params(idempotency_key="abc").bind(
    send_report.using(queue_name="reports")
).enqueue(42)
```

**Typing contract** (interface only — no bodies; implementation is TDD'd):

- `absurd_params` gets two overloads, both explicit keyword-only params, no
  `Unpack`/TypedDict at the public surface (`SpawnKwargs` stays internal, mirroring the
  SDK's `**merged` call as its docstring says).
  - overload 1 — decorator fields → returns the callable-and-bindable form
  - overload 2 — adds `headers`, `idempotency_key` → returns the **bind-only** form
- Bind-only form exposes `bind(target: Task[P, R]) -> Task[P, R]`.
- Callable form extends it, adding `__call__(target: Callable[P, R]) -> Callable[P, R]`.

Why this shape:

- Overload selection is by which keyword set is passed; all fields optional, no
  required-param trick needed (overload 1 simply lacks `headers`/`idempotency_key`, so
  passing either falls through to overload 2).
- `bind` returns `Task[P, R]` → `.enqueue()` keeps full task-arg checking, no
  `[call-arg]`, no namespace squatting.
- A `Task` is **not** `Callable` (measured), so `__call__` can never accept one →
  applying any params above `@task` is a static error. Keeps today's below-`@task` rule
  without frame inspection.
- Keys outside both overloads are rejected statically as unexpected keyword arguments,
  so `absurd_params(queue="reports")` fails at check time by construction — no denylist
  needed for typed users.

## Mechanism

Django's `Task` is `@dataclass(frozen=True, slots=True, kw_only=True)` — nothing can be
attached to an instance. Params ride a **plain (non-dataclass) subclass** declaring its
own slot, registered as the backend's `task_class` (a public backend attribute;
`django/tasks/base.py:144` builds tasks via `task_backends[backend].task_class(...)`),
so a plain `@task()` already yields one — no definition-site change.

Measured properties:

- `dataclasses.replace()` preserves the subclass; result is a new instance, original
  untouched (`absurd_params` reads `None` on it).
- `isinstance(bound, Task)` holds. No `__dict__` (slots intact).
- mypy-clean: a **non**-dataclass subclass dodges the frozen-inheritance check.

Note the alternative that does NOT work: a _dataclass_ subclass is the natural spelling
but django-stubs declares `Task` as `@dataclass(kw_only=True)` — missing
`frozen`/`slots` — so mypy rejects the correct version
(`Frozen dataclass cannot inherit from a non-frozen dataclass [misc]`) while a
non-frozen subclass raises at runtime
(`cannot inherit non-frozen dataclass from a frozen one`). Upstream stubs bug; the
plain-subclass route sidesteps it with no ignore. Worth filing upstream regardless.

Caveat: frozen's generated `__setattr__` raises only when `type(self) is cls` or the
name is a declared field, so the slot is writable on the subclass. Immutability there is
convention — always produce a fresh instance, never mutate.

Internal representation of params: the existing `SpawnKwargs` dict. Both param
dataclasses are deleted; `NotSet`/`NOT_SET` stays as the signature default sentinel.

## Precedence and merge

Highest wins: **per-invocation → task-level default → `DEFAULT_MAX_ATTEMPTS`**
(`max_attempts` only). Already the order `build_merged_spawn_options` implements.

Changes:

- `AbsurdBackend.enqueue` reads per-call params from the task's slot and defaults from
  the function attribute. Stops popping `absurd_spawn_params` from kwargs.
- `pg_cron/reconcile.py:48` reads the renamed function attribute.
- Merge copies nested mutable values. **Insurance, not a requirement**: with `headers`
  per-invocation-only, task-level defaults hold an int plus `retry_strategy` /
  `cancellation`, and the SDK rebuilds both into fresh dicts
  (`_serialize_retry_strategy`, `_normalize_cancellation`). Measured leak that the copy
  forecloses: `.update()` is shallow, so a merged `headers` **is** the source object —
  one in-place write (SDK `before_spawn` hook at `absurd_sdk/__init__.py:1478`, or our
  own future `run_after` header for #116) would persist into every later enqueue in the
  process.

## Guards

| Condition                               | Static                                             | Runtime                                                                                                                           |
| --------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Unknown keyword                         | `[call-overload]` unexpected keyword               | `TypeError`, curated message: `queue`/`queue_name`/`priority`/`backend` → name `.using()`; `run_after` → defer unsupported (#116) |
| Per-invocation field used as decorator  | `"…" not callable [operator]`                      | `TypeError` naming the field + the dedup collapse                                                                                 |
| Any params applied above `@task`        | Task is not `Callable` → `[arg-type]`/`[operator]` | `TypeError` — apply below `@task`                                                                                                 |
| Decorator applied twice to one function | —                                                  | `TypeError` naming fields already set                                                                                             |
| `.bind()` on an already-bound task      | —                                                  | `TypeError` naming fields already set                                                                                             |
| Non-Absurd backend                      | —                                                  | no-op; returns the **input instance** (not a copy); `WARNING` once per task, deduped on `task.module_path` process-wide           |

Runtime guards exist for untyped callers and `**dict` splatting. Implementation
signature spells out all five keyword-only params **plus** `**unsupported`, so
`inspect.signature` stays informative while unknown keys reach our curated message
instead of Python's bare `TypeError`.

Off-backend detection: `isinstance(task.get_backend(), AbsurdBackend)` — `get_backend()`
is a dict lookup (`task_backends[self.backend]`), cheap. Check runs **before** binding,
since a plain Django `Task` has no slot to write. Uses
`logging.getLogger("django_absurd")`; works today, #25 only changes formatting → no
dependency.

## Migration (before → after)

| Site             | Before                                                                     | After                                                                    |
| ---------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| Decorator        | `@absurd_default_params(max_attempts=7)`                                   | `@absurd_params(max_attempts=7)`                                         |
| Per-call         | `add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(max_attempts=9))` | `absurd_params(max_attempts=9).bind(add).enqueue(1, 2)`                  |
| Per-call, strict | same + `# type: ignore[call-arg]`                                          | no ignore needed                                                         |
| With `.using()`  | kwarg inside `.enqueue()` alongside `**kwargs`                             | `absurd_params(...).bind(task.using(...)).enqueue(*args, **kwargs)`      |
| Backend read     | `kwargs.pop(...)` + `getattr(task.func, "absurd_default_params")`          | `getattr(task, "absurd_params")` + `getattr(task.func, "absurd_params")` |

Internal call site to update: `scheduler.py:78` (beat). Collision between
`**schedule.kwargs` and the magic kwarg becomes structurally impossible.

## Docs changes

- `docs/web/tasks.md` "Retries & spawn options" — both examples respelled; add the
  `.using()`-owns-routing note and the off-backend no-op note. Field table stands as-is:
  its "default + per-call" / "per-call only" column already encodes the overload split.
  State that the per-call-only rows are signature-enforced.
- `django_absurd/AGENTS.md` — same two snippets; note the return is an ordinary `Task`.
- `docs/web/cron-jobs.md:244` — mentions `@absurd_default_params` by name.
- Link the decorator fields to Absurd's own
  [task definition](https://earendil-works.github.io/absurd/) defaults
  (`default_max_attempts`, `default_cancellation`). **Do not claim field parity**:
  `register_task` takes no `retry_strategy`, so ours is django-absurd's addition applied
  at spawn.

## Tests (RED first)

Behavioral, through real entrypoints — no monkeypatching, full message assertions.

1. **Decorator default reaches the spawned task.** Enqueue a task carrying
   `@absurd_params(max_attempts=7)`; assert the stored options.
2. **Per-call overrides the default.** Same task,
   `absurd_params(max_attempts=9).bind(...)`; assert 9 lands, and that a later plain
   `.enqueue()` still sees 7 (no leak into the module-level task).
3. **Settings floor applies when neither layer sets it** — `DEFAULT_MAX_ATTEMPTS`.
4. **`headers` / `idempotency_key` per-call** — dedupe: two enqueues with one key spawn
   one task.
5. **Returned object is a `Task`** — `isinstance`, and `aenqueue` works through it.
6. **Original task untouched** after `.bind()`.
7. **Guards** — one test per row of the guard table, asserting complete message text:
   unknown keyword (incl. `queue` → names `.using()`), per-invocation field as
   decorator, application above `@task`, double decorator apply, double bind.
8. **Off-backend no-op** — settings with Django's immediate backend; assert the returned
   task **is** the input instance, the log record via `caplog`, that a second bind of
   the same task logs nothing more, and that enqueue still works.
9. **Typing tests** — a module that must stay mypy-clean for correct usage (both sites,
   task-arg checking preserved), plus negative cases pinned with narrow ignores so
   `warn_unused_ignores` fails the build if a guard regresses.

Rewrites/deletions in `tests/core/test_params.py`: dataclass unit tests (`to_kwargs`) go
— coverage moves to behavioral assertions on spawned options.
`test_decorator_above_task_raises` stays in spirit, respelled. `tests/tasks.py`
decorator usages (5 sites) and the ~12 `AbsurdSpawnParams` call sites across
`tests/core/` respelled.

## Evidence

mypy 2.3.0 strict + django-stubs 6.0.7; runtime probes on Python 3.14 / Django 6.0.

```
# status quo
error: Unexpected keyword argument "absurd_spawn_params" for "enqueue" of "Task"  [call-arg]

# decorator today: neither wrong value type nor typo caught
(no errors)

# overload pair selects per site
Revealed type is "DefaultParams"   <- absurd_params(max_attempts=3)
Revealed type is "TaskParams"      <- absurd_params(headers={...}) / (idempotency_key=...)

# guards
error: "TaskParams" not callable  [operator]                     <- per-invocation as decorator
error: Unexpected keyword argument "queue" for … absurd_params  [call-overload]
error: Incompatible types in assignment (… "Task[[int], str]",
       variable has type "Callable[..., Any]")                   <- Task is not Callable
error: Argument 1 to "enqueue" of "Task" has incompatible type "str"; expected "int"

# mechanism
replace preserves subclass: AbsurdTask
new instance: True   |   bound carries params: {...}   |   ORIGINAL untouched: None
isinstance(Task): True   |   no __dict__ (slots intact): True

# leak (why merge copies)
headers SHARED by reference: True
task-level default now: {'tenant': 'acme', 'injected': 'leaked'}
```

## Follow-ups (not in scope)

- File the django-stubs bug: `Task` stub missing `frozen=True, slots=True`.
- Reserve a header namespace once #116 carries `run_after` in headers.
- No issue exists for this work; closest is #23 (closed). File one.
