# Typed Absurd params: one `absurd_params`, two sites

Status: designed, not built. Tracked in
[#119](https://github.com/lincolnloop/django-absurd/issues/119). Replaces
`@absurd_default_params` and the `absurd_spawn_params=` kwarg.

## Problem

Per-invocation params ride a magic kwarg on `.enqueue()`. Three defects, measured under
mypy 2.3.0 strict / django-stubs 6.0.7:

1. **Untypeable.** `enqueue` is typed `(*args: _P.args, **kwargs: _P.kwargs)`
   (`django-stubs/tasks/base.pyi:50`) where `_P` is the task function's ParamSpec, so no
   outside kwarg can type-check:
   `Unexpected keyword argument "absurd_spawn_params" for "enqueue" of "Task" [call-arg]`.
   Strict users need a per-call ignore; our own suite carries one at
   `tests/core/test_results.py:78`.
2. **Squats the task's kwargs namespace.** `backends.py:106` pops the kwarg before
   spawn, so a task with a parameter of that name silently loses its argument until the
   worker runs — and `scheduler.py:78` splats `**schedule.kwargs` alongside it.
3. **Decorator unchecked.** `params.py:64` is `**kwargs: AbsurdFieldValue`, a union
   containing `str` with no field-name checking, so both `max_attempts="not-an-int"` and
   the typo `max_attemps=3` pass mypy clean.

## Decisions

| Question                   | Decision                                                                                                                                                   |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Settings layer             | `DEFAULT_MAX_ATTEMPTS` stays the only settings-level param (`max_attempts` only). No new `OPTIONS` key.                                                    |
| Decorator field set        | `max_attempts`, `retry_strategy`, `cancellation` — unchanged.                                                                                              |
| Per-invocation field set   | Decorator fields + `headers` + `idempotency_key` — unchanged.                                                                                              |
| Old spellings              | Hard break. `@absurd_default_params`, the kwarg, and both param dataclasses are deleted.                                                                   |
| Return value               | A real `Task`: `isinstance` holds, `aenqueue`/`call`/`get_result`/`using` inherited, original untouched.                                                   |
| Enqueue-site spelling      | `.bind(task)`. Only the decorator-legal form has a usable `__call__`; the per-call form's exists solely to raise a curated message.                        |
| Per-site separation        | Static, via an overload pair on `absurd_params`; runtime guards back it up for untyped callers.                                                            |
| Double apply / double bind | Merge, later value winning per field. No guard — mirrors `Task.using()`. Not intended usage, not documented.                                               |
| Non-Absurd backend         | No-op returning the input instance; `WARNING` once per task, deduped on `task.module_path`.                                                                |
| Ignores in tests           | Any test constructing statically-invalid usage may carry narrow `# type: ignore[...]`; `warn_unused_ignores` fails the build if the error stops occurring. |

## Public surface

`absurd_params` is exported from the package root —
`from django_absurd import absurd_params` — alongside `get_absurd_context` and
`emit_event`, since it is the most-used public symbol. `django_absurd.params` keeps
working as its home module.

```python
from django.tasks import task
from django_absurd import absurd_params


@task
@absurd_params(max_attempts=3)
def send_report(user_id: int) -> None: ...


absurd_params(idempotency_key=f"report:{42}").bind(send_report).enqueue(42)

# Django's own options stay on .using(); order is free
absurd_params(idempotency_key="abc").bind(
    send_report.using(queue_name="reports")
).enqueue(42)
```

Typing contract (interface only — implementation is TDD'd):

- `absurd_params` has two overloads, both explicit keyword-only params. No
  `Unpack`/TypedDict at the public surface; `SpawnKwargs` stays internal, mirroring the
  SDK's `**merged` call.
  - decorator fields → returns `AbsurdParams`
  - plus `headers`/`idempotency_key` → returns `PerCallParams`
- Both are **siblings** over a `ParamsBase` carrying
  `bind(target: Task[P, R]) -> Task[P, R]`. Not a subclass relationship: `PerCallParams`
  declares `__call__(target: Never) -> NoReturn` (to raise the curated definition-site
  message), and `NoReturn` is the bottom type, so no subclass can widen that return —
  mypy rejects it as
  `Return type "Callable[P, R]" of "__call__" incompatible with return type "Never" in supertype [override]`.
- `AbsurdParams` adds the real `__call__(target: Callable[P, R]) -> Callable[P, R]`.

Properties this buys:

- All fields optional; overload selection follows the keyword set, since the first
  overload simply lacks `headers`/`idempotency_key`.
- `bind` returns `Task[P, R]`, so `.enqueue()` keeps full task-arg checking with no
  `[call-arg]` and no namespace squatting.
- A `Task` is not `Callable`, so `__call__` can never accept one — applying params above
  `@task` is a static error, no frame inspection needed.
- Keys outside both overloads are rejected statically, so
  `absurd_params(queue="reports")` fails at check time for typed users.

## Mechanism

`AbsurdTask` is a frozen dataclass subclass of `django.tasks.base.Task` carrying
`absurd_params: SpawnKwargs | None` as a **field**, registered as the backend's
`task_class` (`django/tasks/base.py:144` builds tasks via
`task_backends[backend].task_class(...)`), so a plain `@task()` yields one with no
definition-site change.

A field, not a `__slots__` entry — `Task.using()` is `dataclasses.replace()`, which
rebuilds from fields only. As a field, params survive `using()`/`replace()`, `deepcopy`,
and pickling, and participate in `__eq__`/`repr`.

Cost: **one `# type: ignore[misc]` on the class definition**, because django-stubs
declares `Task` as `@dataclass(kw_only=True)` while the runtime is
`frozen=True, slots=True` — the frozen subclass runtime requires is rejected by mypy,
and a non-frozen one raises at runtime. The ignore self-cleans: mypy is pinned `==2.3.0`
with `warn_unused_ignores`, so a corrected stub turns it into `[unused-ignore]`.

`bind` constructs an `AbsurdTask` from the target's fields; it does not assume the
target already is one. A task defined on another backend keeps `task_class = Task`, and
`.using(backend=...)` preserves that class, so a task routed into the Absurd backend
arrives as a plain `Task` — and the off-backend guard does not catch it, since the
backend _is_ Absurd. `scheduler.py:74` is where such a task would arrive — though a task
defined on the Absurd backend stays an `AbsurdTask` through that routing, so the
plain-`Task` path needs a task defined elsewhere to reach it.

Params are represented internally as the existing `SpawnKwargs` dict. `NotSet`/`NOT_SET`
remains the signature default sentinel; `absurd_params()` with no fields is legal and
yields no params.

The field needs no privacy wrapper: `frozen` already makes it read-only
(`FrozenInstanceError cannot assign to field 'absurd_params'`) and it defaults to `None`
on unbound tasks. A `_absurd_params` field behind a property would only move the
underscore into the constructor keyword.

**One read location.** The decorator writes a function attribute because it has no task
yet, but `AbsurdTask.__post_init__` folds that into the field, so `enqueue` and
`reconcile` both consult a single attribute. The fold uses `setdefault` semantics —
defaults fill absent keys only — which makes it idempotent: `.using()` is `replace()`,
which re-runs `__post_init__`, and an overwriting fold would silently clobber per-call
values on any `bind(...).using(...)`.

No double-apply guards. Stacking the decorator or binding twice merges, later value
winning per field — consistent with `Task.using()`, which is freely re-appliable. Not
intended usage and not a feature we document; we simply don't care if someone does it.
`bind` lives only on the params object; nothing is added to the task's surface.

`absurd_params` is a bare noun, which CLAUDE.md's verb rule normally forbids. Accepted:
it is a decorator reused at both sites, and `bind` carries the verb. `bind` over `apply`
(Celery's `apply()` executes a task — actively misleading), `attach` (implies mutating
the target), `on` (vague), `for_task` (stutters before `.enqueue`).

## Precedence and merge

Highest wins: **per-invocation → task-level default → `DEFAULT_MAX_ATTEMPTS`**
(`max_attempts` only) — the order `build_merged_spawn_options` already implements.

- `AbsurdBackend.enqueue` reads per-call params behind `isinstance(task, AbsurdTask)`,
  which narrows to the fully-typed field (`SpawnKwargs | None`) — no `getattr`, no
  `Any`, no ignore, and reading it off a bare `Task` is a static error. Task-level
  defaults stay a function-attribute read, since a function cannot declare attributes;
  that is where `params.py`'s existing `[attr-defined]` ignore lives. The `kwargs.pop`
  goes away.
- `pg_cron/reconcile.py:48` has its own merge path reading the function attribute; it
  gets the same rename and its own `setdefault` floor stays.
- The merge copies nested mutable values. Insurance rather than a requirement today,
  since `headers` is per-invocation-only and the SDK rebuilds
  `retry_strategy`/`cancellation` into fresh dicts (`_serialize_retry_strategy`,
  `_normalize_cancellation`) while passing `headers` by reference. It becomes a
  requirement the moment anything writes a header in-place — the SDK's `before_spawn`
  hook (`absurd_sdk/__init__.py:1478`) or #116's planned `run_after` header.
- **Both `setdefault("max_attempts", …)` calls stay.** They are not duplicating the
  SDK's own `default_max_attempts = 5`; they are the infinite-retry guard.
  `absurd.fail_run` retries while `v_max_attempts is null`, and `spawn_task` only reads
  the key when present (`if p_options ? 'max_attempts'`), so an omitted value lands as
  NULL = retry forever. This already bit us on a pg_cron schedule. The same reason keeps
  `ScheduledTask.max_attempts`'s field default a concrete number.
- The SDK carries its own fallbacks in `_prepare_spawn` (`registration` defaults, then
  `self._default_max_attempts = 5`). They never fire because `build_absurd_client` makes
  a registry-empty client per enqueue and both merge paths `setdefault` `max_attempts`
  unconditionally. Preserve both invariants or the SDK's 5 silently substitutes for
  `DEFAULT_MAX_ATTEMPTS`.

## Guards

**The runtime layer is the contract; the static layer is a bonus.** Users may run no
type checker at all, so every rule here raises a correct, self-explanatory Python error
on its own — no guard may rely on a checker having caught it first.

| Condition                              | Static                                    | Runtime                                                     |
| -------------------------------------- | ----------------------------------------- | ----------------------------------------------------------- |
| Unknown / unsupported keyword          | `[call-overload]`                         | curated `TypeError` (below)                                 |
| Per-invocation field used as decorator | `[arg-type]` — `__call__` accepts `Never` | curated `TypeError` (below)                                 |
| Params applied above `@task`           | `Task` is not `Callable` → `[arg-type]`   | curated `TypeError` — apply below `@task`                   |
| `bind` on a non-`Task`                 | `[arg-type]`                              | curated `TypeError` pointing at the decorator form          |
| Non-Absurd backend                     | —                                         | no-op returning the input instance; `WARNING` once per task |

**No value validation on this surface.** `absurd_params` does not check that
`max_attempts` is an `int`, that `retry_strategy["kind"]` is a known value, or anything
else about the data. These fail loudly on their own — `max_attempts=0` gets
`max_attempts must be >= 1` from Postgres, `max_attempts="nope"` fails the cast — and
re-stating the SDK's types here is duplicated policy that drifts from the pinned SQL.

A judgment about _this_ surface, not a blanket rule: configuration still gets system
checks (`absurd.E009` on `DEFAULT_MAX_ATTEMPTS`) and persisted user data still gets
validators (pg_cron schedule grammar, `full_clean`).

Two accepted consequences around `retry_strategy`, both to be recorded in a comment at
its declaration rather than guarded:

- **An unrecognized `kind` is validated nowhere.** `fail_run` does
  `coalesce(v_retry_strategy->>'kind', 'none')` and falls through to
  `v_delay_seconds := 0`, so a typo retries with no backoff instead of erroring. Type
  checkers catch it — the SDK types `kind` as a `Literal`.
- **An omitted `kind` raises, though the type says it is optional.** `RetryStrategy` is
  `TypedDict, total=False`, but `_serialize_retry_strategy` does `strategy["kind"]`
  unconditionally, so `retry_strategy={"base_seconds": 5}` type-checks under both mypy
  and pyright and then dies at enqueue with `KeyError: 'kind'` from inside `absurd_sdk`.
  No checker can flag this; the SDK's type is simply wrong. Left to error — validating
  it is YAGNI complexity for now, revisitable if users ask.

Also worth knowing, same source: the SDK silently **drops** unknown keys in
`retry_strategy` and `cancellation` (a typo'd `factor` is a no-op), and passes bad value
types straight through — a non-numeric `base_seconds` reaches the payload and only fails
inside `fail_run`, i.e. on the first retry rather than at enqueue.

What the guards above do cover is a different category — mistakes where the caller is at
the wrong door, not holding the wrong data. Python's own message can't point at the
right API, so those are curated.

Unsupported keywords arrive through `**unsupported` in the implementation signature, so
they are freely worded; the factory runs before any target exists, so those cannot name
the task. One plain shape for anything unrecognized — no fuzzy matching, no suggestions:

```
TypeError: 'max_attemps' is an invalid argument for absurd_params. Valid arguments:
max_attempts, retry_strategy, cancellation, headers, idempotency_key.
```

Routing keys get the extra pointer, because they are real Absurd spawn options
(`spawn(queue=...)`) — the message must say they are not allowed _here_, not that they
do not exist. `queue_name` shares it: a user reaching for Django's spelling is at the
same wrong door.

```
TypeError: 'queue' cannot be set through absurd_params — queue routing belongs to Django's Task API:

    send_report.using(queue_name="reports")
```

`run_after` gets **no** pointer, and that is the rule working rather than an exception
to it. It is not an Absurd spawn option at all — `spawn` takes `max_attempts`,
`retry_strategy`, `headers`, `queue`, `cancellation`, `idempotency_key` — it is purely a
`Task.using()` kwarg. So it falls through to the generic invalid-argument message. Used
properly it raises Django's own `InvalidTask("Backend does not support run_after.")`,
since this backend sets `supports_defer = False`; the deferred-enqueue limitation is
documented in `AGENTS.md` alongside the other backend capabilities.

The definition-site message does know its target:

```
TypeError: idempotency_key can only be set per invocation, not on a task definition.
Bind it at enqueue time instead:

    absurd_params(idempotency_key=...).bind(send_report).enqueue(...)
```

Getting that message requires `__call__` to exist while remaining a static error, so it
is declared as accepting `t.Never` and returning `t.NoReturn`: mypy rejects every call
(`expected "Never"`), and the body raises the curated message. Declaring `__call__` only
under `if not t.TYPE_CHECKING` would yield a tidier static message (`not callable`) at
the cost of telling the checker something false about runtime; rejected on those
grounds, and the runtime message is identical either way.

The implementation signature spells out all five keyword-only params plus
`**unsupported`, so `inspect.signature` stays informative while unknown keys reach the
curated message instead of Python's bare `TypeError`. Runtime guards exist for untyped
callers and `**dict` splatting, which evades static checking.

Stacked decorators and repeated binds are not guarded — they merge (see Mechanism).

Off-backend detection is `isinstance(task.get_backend(), AbsurdBackend)` —
`get_backend()` is a dict lookup. Logging goes to `logging.getLogger("django_absurd")`,
which works today; #25 only changes formatting.

The decorator site has no such guard, since it attaches before any backend is known. A
task carrying only decorator defaults on a non-Absurd backend loses them silently.
Accepted: the params are inert either way, and warning at decoration would fire at
import for every task in projects that swap backends per environment.

## Migration

| Site             | Before                                                                     | After                                                                                  |
| ---------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Decorator        | `@absurd_default_params(max_attempts=7)`                                   | `@absurd_params(max_attempts=7)`                                                       |
| Per-call         | `add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(max_attempts=9))` | `absurd_params(max_attempts=9).bind(add).enqueue(1, 2)`                                |
| Per-call, strict | same + `# type: ignore[call-arg]`                                          | no ignore                                                                              |
| With `.using()`  | kwarg inside `.enqueue()` alongside `**kwargs`                             | `absurd_params(...).bind(task.using(...)).enqueue(*args, **kwargs)`                    |
| Backend read     | `kwargs.pop(...)` + `getattr(task.func, "absurd_default_params", None)`    | `isinstance(task, AbsurdTask)` narrowing + `getattr(task.func, "absurd_params", None)` |

Internal call site: `scheduler.py:78` (beat).

## Docs changes

- `docs/web/tasks.md` "Retries & spawn options" — respell both examples; add the
  `.using()`-owns-routing and off-backend no-op notes. The field table stands: its
  "default + per-call" / "per-call only" column already encodes the overload split,
  which is now signature-enforced.
- `django_absurd/AGENTS.md` — same two snippets; note the return is an ordinary `Task`.
  Its module docstring list in `__init__.py` mentions "enqueue params/decorators" and
  now also exports the symbol.
- `docs/web/cron-jobs.md:244` — names `@absurd_default_params`.
- Examples import from the package root (`from django_absurd import absurd_params`).
- Link the decorator fields to Absurd's
  [task definition](https://earendil-works.github.io/absurd/) defaults
  (`default_max_attempts`, `default_cancellation`) without claiming field parity:
  `register_task` takes no `retry_strategy`, so that one is ours, applied at spawn.

## Tests (RED first)

Behavioral, through real entrypoints. No monkeypatching; assert complete message text.

1. Decorator default reaches the spawned task.
2. Per-call overrides the default, and a later plain `.enqueue()` of the same task still
   sees the default.
3. `DEFAULT_MAX_ATTEMPTS` applies when neither layer sets `max_attempts`.
4. `headers` and `idempotency_key` per call; two enqueues with one key spawn one task.
5. The bound object is a `Task` — `isinstance`, and `aenqueue` works through it.
6. Params survive `.bind(task).using(queue_name=...)` — both orderings reach the spawned
   task identically, and a decorator default is not clobbered by the re-fold `replace()`
   triggers.
7. A task defined on a non-Absurd backend and routed in via `.using(backend=...)` binds
   and spawns with its params.
8. Repeated binds merge, later value winning per field; a stacked decorator merges the
   same way.
9. One test per guard row.
10. Off-backend no-op: an immediate-backend task returns the input instance, logs once,
    stays quiet on a second bind, and still enqueues and runs to completion. Mixed
    backends (one Absurd, one immediate) routed via `.using(backend=...)`. Uses a task
    no other test touches, since the dedup is process-wide and xdist-order-sensitive.
11. Typing tests: correct usage at both sites stays mypy-clean; negative cases pinned
    with narrow ignores. Keep the negative expressions out of collected test bodies (or
    inside `pytest.raises`) — they raise at import otherwise.

12. An unset field is **omitted** from the spawn payload, not sent as null. Replaces the
    deleted `test_to_kwargs_emits_only_set_fields`, whose intent nothing else covers.

Items 1–4 already exist and their assertions are stable — `test_enqueue.py` covers all
three precedence layers (5 / 7 / 9) plus headers, retry strategy, and dedupe, and
`tests/pg_cron/test_pg_cron_options.py` covers the reconcile path's own merge including
"7, not 5" (our `DEFAULT_MAX_ATTEMPTS` beating the SDK's default). Items 5–12 are new.

### Existing-test inventory

37 references across 10 files. Nine files are pure syntax — no assertion moves:

| File                                    | Refs | Change                                           |
| --------------------------------------- | ---- | ------------------------------------------------ |
| `tests/tasks.py`                        | 6    | 5 decorator lines + import                       |
| `tests/core/test_enqueue.py`            | 7    | 6 call sites + import                            |
| `tests/core/test_durable.py`            | 3    | call sites                                       |
| `tests/core/test_results.py`            | 2    | call site; its `type: ignore[call-arg]` goes too |
| `tests/core/test_async_worker.py`       | 2    | call site                                        |
| `tests/core/test_orm_models.py`         | 2    | call site                                        |
| `tests/core/test_pytest_plugin.py`      | 2    | call site                                        |
| `tests/core/test_admin/utils.py`        | 2    | call site                                        |
| `tests/pg_cron/test_pg_cron_options.py` | 1    | comment only                                     |

`tests/core/test_params.py` (10 refs) is the exception. Correct usage behaves
identically, and decorator misuse still fails at import with a `TypeError`. What changes
is which expression raises it:

- `test_to_kwargs_emits_only_set_fields` and
  `test_spawnparams_carries_per_invocation_fields` — subject deleted. The second is
  covered by `test_headers_reach_spawn` / `test_idempotency_key_dedups`; the first is
  why item 12 exists.
- `test_decorator_rejects_per_invocation_kwarg` — rewritten, not respelled. Today the
  bare factory call raises (the dataclass rejects the field); now it is legal, because
  that is the per-call form `bind` needs. The test must apply the result to a function
  to trigger the failure. For a user writing the decorator nothing observable changes:
  both the call and the application sit on one line, run at import, and raise
  `TypeError` — only the message differs.
- `test_decorator_attaches_default_to_task_func` — asserts internals; deleted in favour
  of the existing behavioral `test_max_attempts_uses_decorator_default`.
- `test_decorator_above_task_raises` — survives; still `TypeError`, new message.

`test_spawn_params_not_passed_to_task_func` stays as a regression guard, though after
the change the separation is structural.

## Alternatives considered

| Alternative                                       | Rejected because                                                                                                                                    |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Keep the `absurd_spawn_params=` kwarg             | Cannot type-check under any spelling, and squats the task's kwargs namespace. The two defects that motivate this work.                              |
| Extend Django's `.using()` with Absurd kwargs     | Fixed keyword-only signature → `[call-overload]` plus a runtime `TypeError`. Overriding it means forking a Django primitive's signature.            |
| Store params in `__slots__` instead of a field    | `.using()` drops them silently — no static or runtime error — and erases the double-bind signal. Also lost on `deepcopy`/pickle, invisible to `eq`. |
| A delegating wrapper object instead of a `Task`   | Not a `Task`: `isinstance` fails, static type differs, and `call`/`get_result` need hand-delegation.                                                |
| Mutate the task in place                          | `frozen=True, slots=True` forbids it, and process-global params would leak into every later enqueue.                                                |
| Decorator-only, with callables for dynamic values | Cannot express request-scoped values, and tying a callable to the task's own ParamSpec is impractical.                                              |
| `Unpack[TypedDict]` for the public signature      | Explicit params give better errors (mypy prints both variants), and work with `inspect.signature` and editors without PEP 692 support.              |
| A fluent `task.with_params(...)` method           | django-stubs types `task()` as returning plain `Task[_P, _R]`, so subclass methods are invisible to type checkers.                                  |

## Follow-ups

- Reserve a header namespace when #116 starts carrying `run_after` in headers.
- Report upstream to Absurd: `RetryStrategy["kind"]` should be `Required`, or
  `_serialize_retry_strategy` should default it to `"none"` the way `fail_run` already
  does. Currently a type-legal value raises `KeyError`.
