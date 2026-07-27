# Typed Absurd params: one `absurd_params`, two sites

Status: designed, not built. Replaces `@absurd_default_params` and the
`absurd_spawn_params=` kwarg.

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

| Question                   | Decision                                                                                                                         |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Settings layer             | `DEFAULT_MAX_ATTEMPTS` stays the only settings-level param (`max_attempts` only). No new `OPTIONS` key.                          |
| Decorator field set        | `max_attempts`, `retry_strategy`, `cancellation` — unchanged.                                                                    |
| Per-invocation field set   | Decorator fields + `headers` + `idempotency_key` — unchanged.                                                                    |
| Old spellings              | Hard break. `@absurd_default_params`, the kwarg, and both param dataclasses are deleted.                                         |
| Return value               | A real `Task`: `isinstance` holds, `aenqueue`/`call`/`get_result`/`using` inherited, original untouched.                         |
| Enqueue-site spelling      | `.bind(task)`. `__call__` exists only for the decorator site, so the two never overlap.                                          |
| Per-site separation        | Static, via an overload pair on `absurd_params`; runtime guards back it up for untyped callers.                                  |
| Double apply / double bind | Merge, later value winning per field. No guard — mirrors `Task.using()` and enables composition.                                 |
| Non-Absurd backend         | No-op returning the input instance; `WARNING` once per task, deduped on `task.module_path`.                                      |
| Typing tests               | Negative cases pinned with narrow `# type: ignore[...]` under `warn_unused_ignores`. Authorized for the typing-test module only. |

## Public surface

```python
from django.tasks import task
from django_absurd.params import absurd_params


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
  - plus `headers`/`idempotency_key` → returns `BoundParams`
- `BoundParams` exposes `bind(target: Task[P, R]) -> Task[P, R]`.
- `AbsurdParams` extends it, adding
  `__call__(target: Callable[P, R]) -> Callable[P, R]`.

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
backend _is_ Absurd. `scheduler.py:74` hits this path on every scheduled task.

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
winning per field — consistent with `Task.using()`, which is freely re-appliable, and it
makes composition legal (`bind` a partially-configured task, specialize it later).
Getting it wrong is the caller's problem. `bind` lives only on the params object;
nothing is added to the task's surface.

`absurd_params` is a bare noun, deviating from CLAUDE.md's verb rule for function names.
Deliberate, for the symmetry of one name at both sites.

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

| Condition                              | Static                                  | Runtime                                                                                                |
| -------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Unknown keyword                        | `[call-overload]`                       | `TypeError`; `queue`/`queue_name`/`priority`/`backend` name `.using()`, `run_after` cites defer (#116) |
| Per-invocation field used as decorator | `"BoundParams" not callable [operator]` | Python's `TypeError: 'BoundParams' object is not callable`                                             |
| Params applied above `@task`           | `Task` is not `Callable` → `[arg-type]` | `AbsurdParams`: curated "apply below `@task`"; `BoundParams`: Python's not-callable                    |
| Non-Absurd backend                     | —                                       | no-op returning the input instance; `WARNING` once per task                                            |

The implementation signature spells out all five keyword-only params plus
`**unsupported`, so `inspect.signature` stays informative while unknown keys reach the
curated message instead of Python's bare `TypeError`. Runtime guards exist for untyped
callers and `**dict` splatting, which evades static checking.

Rows 2 and 3 deliberately accept Python's own message: the static `[operator]` error
exists _because_ `BoundParams` has no `__call__`, and adding one for nicer wording would
destroy it.

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
- `docs/web/cron-jobs.md:244` — names `@absurd_default_params`.
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

`tests/core/test_params.py`: the `to_kwargs` dataclass unit tests go, replaced by
behavioral assertions on spawned options; the above-`@task` test is respelled.
`tests/tasks.py` has 5 decorator sites; `tests/core/` has ~13 kwarg call sites.

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
- File a GitHub issue for this work; none exists (closest is #23, closed).
