# django-absurd — Spec: Absurd spawn params (SP5)

Date: 2026-06-22 Status: approved-for-planning

Thread Absurd's `spawn` params through `AbsurdBackend.enqueue`. SP2 passed only
`args`/`kwargs` + `max_attempts=self.default_max_attempts`. Absurd's `spawn` also
accepts `retry_strategy`, `headers`, `cancellation`, `idempotency_key` (and per-call
`max_attempts`). Django's `Task` has no slot for them.

TWO typed objects, split by lifecycle:

- **`AbsurdDefaultParams`** — task-level defaults (the subset that makes sense
  task-wide). Set via the `@absurd_default_params(...)` decorator on the task.
- **`AbsurdSpawnParams`** — the full per-enqueue set (defaults + the per-invocation-only
  fields). Passed as the reserved `absurd_spawn_params=AbsurdSpawnParams(...)` kwarg.

Both fold into the SAME `spawn` call at enqueue. There is NO separate "registration"
path: Absurd's `register_task` defaults are resolved at SPAWN time on the producer
(`_prepare_spawn`, SDK `__init__.py:1252` — explicit arg > registry default > client
default) and never consulted at worker execution; our producer client carries an empty
registry, so the only place a task-level default can take effect is folded into the
spawn args at enqueue. The worker / `LazyTaskRegistry` is NOT touched by SP5.

**Deliberate deviation from Absurd's API (for the better):** Absurd exposes these only
as per-spawn args. We reduce surface by splitting them — task-level defaults
(`AbsurdDefaultParams`) vs the full per-enqueue set (`AbsurdSpawnParams`) — so the
per-invocation-only fields simply don't exist on the decorator's type.

## The spawn parameters (split by lifecycle)

`spawn(task_name, params, max_attempts, retry_strategy, headers, queue, cancellation, idempotency_key)`
([docs](https://earendil-works.github.io/absurd/sdks/python/#spawning-tasks)).

- **Backend-owned, NOT exposed:** `task_name` (= `task.module_path`), `params` (=
  `{"args","kwargs"}`), `queue` (= `task.queue_name`).

| Param             | `AbsurdDefaultParams` (decorator) | `AbsurdSpawnParams` (enqueue) |
| ----------------- | :-------------------------------: | :---------------------------: |
| `max_attempts`    |                ✅                 |              ✅               |
| `retry_strategy`  |                ✅                 |              ✅               |
| `cancellation`    |                ✅                 |              ✅               |
| `headers`         |                ❌                 |              ✅               |
| `idempotency_key` |                ❌                 |              ✅               |

`headers` (per-instance metadata — trace/request id) and `idempotency_key` (dedup key
for ONE enqueue) make no sense as a task-wide constant, so they live only on
`AbsurdSpawnParams`. Passing one to the decorator → `TypeError` from
`AbsurdDefaultParams` (unknown kwarg) for free.

## NOT_SET sentinel + `to_kwargs`

Every field defaults to a module-level `NOT_SET` sentinel (NOT `None`). `to_kwargs()`
emits only fields `is not NOT_SET`, so an unset field is never passed and Absurd's own
default stands. No `| None` in field types — a field holds exactly the Absurd-documented
type or is absent.

```
NOT_SET: t.Any = object()   # typed Any so `x: int = NOT_SET` needs no type:ignore
```

## Typed objects (in `django_absurd/params.py`)

Field types are the exact Absurd spawn-param types:
`from absurd_sdk import RetryStrategy, CancellationPolicy, JsonObject`
(`RetryStrategy`/`CancellationPolicy` are `TypedDict`s,
`JsonObject = Dict[str, JsonValue]` — valid annotations; STATIC typing only, no runtime
value validation). Unknown kwarg → `TypeError` from the constructor for free (typo
`idempotancy_key=`) — the only runtime check; field types do NOT validate values.
`AbsurdSpawnParams` EXTENDS `AbsurdDefaultParams`, so `to_kwargs` + the shared fields
live once.

```
@dataclass(frozen=True)
class AbsurdDefaultParams:
    """Absurd spawn defaults settable per-task via @absurd_default_params(...).

    Only fields you set are forwarded to Absurd.spawn; unset fields keep Absurd's defaults.
    See https://earendil-works.github.io/absurd/sdks/python/#spawning-tasks
    """
    max_attempts: int = NOT_SET
    retry_strategy: RetryStrategy = NOT_SET
    cancellation: CancellationPolicy = NOT_SET

    def to_kwargs(self) -> dict[str, t.Any]:
        return {f.name: getattr(self, f.name)
                for f in dataclasses.fields(self)
                if getattr(self, f.name) is not NOT_SET}


@dataclass(frozen=True)
class AbsurdSpawnParams(AbsurdDefaultParams):
    """Full per-enqueue Absurd spawn params. Pass as the reserved `absurd_spawn_params`
    kwarg to Task.enqueue; per-enqueue overrides per-task defaults, per field."""
    headers: JsonObject = NOT_SET          # per-invocation only
    idempotency_key: str = NOT_SET         # per-invocation only
```

## Entry point 1 — per-task default (decorator `@absurd_default_params`)

`@task` takes only fixed kwargs — cannot carry these. Companion decorator
`absurd_default_params(**kwargs)` builds `AbsurdDefaultParams(**kwargs)` (constructor
rejects unknown / per-invocation kwargs at decoration) and attaches it to the FUNCTION:

```python
@task
@absurd_default_params(max_attempts=5)
def foo(...): ...
```

- Decorates the raw FUNCTION (must sit BELOW `@task`): sets
  `func.absurd_default_params = <the AbsurdDefaultParams>`; returns `func` unchanged.
  `@task` then wraps it, so `task.func` carries the attr (verified:
  `task(raw).func is raw`, and an attr set on `raw` before `@task` survives as
  `task.func.<attr>`).
- **Wrong order raises:** applied ABOVE `@task` it receives a `Task`, not a function →
  raise `TypeError` ("apply @absurd_default_params below @task"). Loud on misuse; no
  silent reach into `Task.func`. The check needs a RUNTIME
  `from django.tasks import Task` (NOT the `TYPE_CHECKING`-only import `backends.py`
  uses) for the `isinstance`.

## Entry point 2 — per-enqueue override (reserved kwarg)

```python
foo.enqueue(2, 3, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="order-42", max_attempts=9))
```

`Task.enqueue(*args, **kwargs)` forwards verbatim to
`backend.enqueue(task, args, kwargs)` (no Django-side signature validation — verified).
`aenqueue` is `sync_to_async(self.enqueue)` so it routes through the same path.
`AbsurdBackend.enqueue` **pops** `absurd_spawn_params` out of `kwargs` BEFORE building
the params dict, so it never reaches `task.func`. `absurd_spawn_params` is a documented
RESERVED kwarg name.

## Merge + precedence (per key, highest wins)

In `enqueue`:

```
default  = getattr(task.func, "absurd_default_params", None)     # AbsurdDefaultParams | None
per_call = kwargs.pop("absurd_spawn_params", None)               # AbsurdSpawnParams | None
merged   = {**(default.to_kwargs()  if default  else {}),
            **(per_call.to_kwargs() if per_call else {})}        # per-call key wins
attempts = merged.pop("max_attempts", self.default_max_attempts)
client.spawn(task.module_path, {"args": ..., "kwargs": ...},
             queue=task.queue_name, max_attempts=attempts, **merged)
```

Precedence per key: per-call `AbsurdSpawnParams` > `@absurd_default_params` default >
(for `max_attempts`) backend `OPTIONS["DEFAULT_MAX_ATTEMPTS"]`. Other keys: set at some
layer → passed; unset everywhere → SDK default. (`headers`/`idempotency_key` only ever
come from the per-call layer.)

The existing unprovisioned-queue catch (`UndefinedTable`/`UndefinedFunction`/
`InvalidSchemaName` → `ImproperlyConfigured`) wraps the same `spawn` call, unchanged.

## Testing (pytest, function-based, real Postgres; highest-level)

Observability (verified against SDK): `TaskResultSnapshot` (`fetch_task_result`) exposes
only `state`/`result`/`failure`. The readable surface for the params is
`client.claim_tasks` → `ClaimedTask`, which carries `max_attempts`, `headers`,
`retry_strategy` (NOT `cancellation`). So tests assert via a claim. (A claim consumes
the row — the dedup test and claim-inspection tests must use distinct tasks/keys.)

- **idempotency_key (dedup, per-enqueue):** enqueue same key twice → ONE task (same
  `task_id` from the two `SpawnResult`s / single queued row). Strongest pass-through
  proof.
- **max_attempts precedence (claim):** enqueue with backend default only / with
  `@absurd_default_params(max_attempts=D)` / with per-call
  `AbsurdSpawnParams(max_attempts=P)`; `claim_tasks` and assert
  `ClaimedTask["max_attempts"]` == backend default / D / P.
- **max_attempts precedence (worker end-to-end, BEST-EFFORT):** the claim test above is
  the authoritative precedence proof. ADDITIONALLY, if feasible, prove it end-to-end: a
  failing task with `@absurd_default_params(max_attempts=D)`, enqueued with per-call
  override `AbsurdSpawnParams(max_attempts=P)` (P≠D); run the worker (SP3
  `run_absurd_worker` burst) and count actual attempts == P. The override won through
  dispatch+retry. Counting via `caplog` (`build_handler` logs per attempt) is one
  option; if burst-mode retry timing makes it unreliable (retry backoff must be 0 for
  immediate re-claim), count attempts another way or DROP this test — the claim test
  already pins precedence. Do not block the plan on it.
- **headers (claim, per-enqueue):** enqueue with `headers={...}`; assert
  `ClaimedTask["headers"]`.
- **retry_strategy (claim):** enqueue with a `retry_strategy` (via decorator and/or
  per-call); assert `ClaimedTask["retry_strategy"]`.
- **cancellation (no read path):** not surfaced by any public read API — cover only via
  `AbsurdSpawnParams(cancellation=...).to_kwargs()` + a passing enqueue (spawn accepts
  it). State the limitation explicitly.
- **decorator rejects per-invocation fields:**
  `@absurd_default_params(idempotency_key="x")` and
  `@absurd_default_params(headers={...})` → `TypeError` (unknown kwarg on
  `AbsurdDefaultParams`).
- **reserved-kwarg isolation:** task func records received kwargs; pass
  `absurd_spawn_params=...`; assert the func does NOT receive it (popped pre-params).
- **decorator correct order works / wrong order raises:** `@absurd_default_params` BELOW
  `@task` → `task.func.absurd_default_params` set; ABOVE `@task` → `TypeError` at
  decoration.
- **NOT_SET serialization:**
  `AbsurdSpawnParams(max_attempts=3).to_kwargs() == {"max_attempts": 3}` — unset fields
  omitted.
- **typed validation:** `TypeError` at `AbsurdSpawnParams(idempotancy_key="x")`.
- Existing enqueue tests stay green (no params → empty merge → unchanged,
  `max_attempts=default_max_attempts`).

No mocks; real DB: `docker compose up -d db`, `PGPORT=5433`.

## Out of scope (deferred, unchanged)

Backend-wide `OPTIONS` defaults; native `register_task` registration on the producer;
result retrieval (`get_result`); native async; `run_after`/defer; priority;
savepoint-on-enqueue-error.
