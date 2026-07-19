# Durable Steps + Sleep — Design

**Goal:** expose Absurd's durable-execution primitives **Steps** + **Sleep** to
django-absurd task functions. A `takes_context=True` task can checkpoint work (`step` —
run, persist result, replay from cache) and durably suspend (`sleep_for`/`sleep_until` —
release the worker, resume later). Turns django-absurd from at-least-once
fire-and-forget into a workflow engine.

Grounded in Absurd docs
([Concepts → Steps](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints),
[Sleep](https://earendil-works.github.io/absurd/concepts/),
[Python SDK](https://earendil-works.github.io/absurd/sdks/python/)) and the SDK source
(`absurd_sdk/__init__.py`). Adversarially reviewed (Fable) before this revision — all
findings folded in.

## Scope

IN: expose ctx to `takes_context=True` tasks; `step`, `sleep_for`, `sleep_until`,
`heartbeat`, `headers` (all on both sync + async variants); `run_step` (sync variant
only — mirrors the SDK); fix `SuspendTask`/`CancelledTask`/`FailedTask` mislogging;
admin visibility (steps under the task); replay-semantics docs + a runnable example.

OUT (own future specs): **Events** pillar (`await_event` + app-side `emit_event`, + the
Waits admin inline it populates); `await_task_result` (cross-queue child + deadlock
guard); sync-worker mode. Async worker + bridge cover both task kinds.

## Replay semantics (the load-bearing contract)

Absurd durable execution: on every retry/resume the **whole task body re-runs
top-to-bottom**. Completed `step(name, ...)` calls return their cached value instead of
re-executing. Sleep flips the run to `sleeping` + reschedules it + raises `SuspendTask`;
on wake the same replay applies.

**Steps are _effectively-once_, NOT exactly-once.** `step` = `begin_step` → run `fn()` →
`complete_step` (persists the checkpoint _after_ `fn` returns), on the worker's
dedicated autocommit connection — so it can never be atomic with the user's Django-ORM
writes (different connection). A crash / claim-timeout between `fn`'s side effect and
the persist re-executes the step. So: persisted-once, **executed at-least-once** in the
crash window. Teaching an absolute "runs once" guarantee produces the exact wrong mental
model.

Docs must teach the full footgun set (see Docs): effectively-once; deterministic step
naming/order; JSON-serializable step returns; don't swallow `SuspendTask`; long steps vs
`claim_timeout`; ctx methods are absurd-only.

## Context exposure — extend Django `TaskContext`, match ctx to task kind

Today worker hands `takes_context=True` tasks a plain Django `TaskContext`
(`task_result` only); Absurd SDK ctx withheld (`worker.py:216`). Change: hand a
django-absurd context that **subclasses Django `TaskContext`** (keeps
`.task_result`/`.attempt`) AND adds the durable methods (delegating to the live Absurd
ctx).

Two variants, chosen by `inspect.iscoroutinefunction(task.func)` — mirrors the SDK's own
`TaskContext` (sync) vs `AsyncTaskContext` (async) split, including the SDK's own
asymmetry (`run_step` is sync-only; everything else on both). Absurd's Python docs are
sync-first, so sync tasks must feel native (no `await`).

**Runtime-subscript trap (C1):** Django `TaskContext` is
`@dataclass(frozen=True, slots=True, kw_only=True)` and is **NOT subscriptable at
runtime** (`TaskContext[...]` → `TypeError`; the `Generic` params live only in
django-stubs). So use the project's `TYPE_CHECKING` conditional-base alias (as
`admin.py:29-32/79-82/336-339` already do), and make each subclass itself a frozen +
slots + kw_only dataclass declaring its extra fields:

```python
if t.TYPE_CHECKING:
    _TaskContextBase = TaskContext[t.Any, t.Any]
else:
    _TaskContextBase = TaskContext

@dataclass(frozen=True, slots=True, kw_only=True)
class AbsurdTaskContext(_TaskContextBase):        # sync — Absurd's primary style, no await
    absurd_ctx: t.Any                          # live SDK AsyncTaskContext
    loop: asyncio.AbstractEventLoop            # worker loop, for bridging
    @property
    def headers(self) -> Mapping[str, JsonValue]: ...
    def step(self, name: str, fn: Callable[[], R]) -> R: ...
    def run_step(self, name_or_fn=None): ...   # decorator; sync-only, mirrors SDK
    def sleep_for(self, name: str, seconds: float) -> None: ...
    def sleep_until(self, name: str, when: datetime | int | float) -> None: ...
    def heartbeat(self, seconds: int | None = None) -> None: ...

@dataclass(frozen=True, slots=True, kw_only=True)
class AsyncAbsurdTaskContext(_TaskContextBase):   # async
    absurd_ctx: t.Any
    @property
    def headers(self) -> Mapping[str, JsonValue]: ...
    async def step(self, name: str, fn: Callable[[], Awaitable[R]]) -> R: ...
    async def sleep_for(self, name: str, seconds: float) -> None: ...
    async def sleep_until(self, name: str, when: datetime | int | float) -> None: ...
    async def heartbeat(self, seconds: int | None = None) -> None: ...
    # NO run_step — the decorator's run-at-decoration + rebind needs sync execution;
    # async can't await at decoration. Matches the SDK omission.
```

Names/args mirror the SDK 1:1 (no invented API; Absurd docs transfer). Step return `R`
is a **JSON value** — persisted via `json.dumps` (see footguns). Both classes
strict-typed. Live in a new `django_absurd/context.py`; public import
`from django_absurd import AbsurdTaskContext, AsyncAbsurdTaskContext` (re-export in
`__init__.py`). Names provisional — settle at plan time.

Usage:

```python
@task(takes_context=True)
def workflow(ctx, order_id):
    charge = ctx.step("charge", lambda: charge_card(order_id))  # persisted; cached on replay
    ctx.sleep_for("cooldown", 5)                                # durable suspend + resume
    ctx.step("ship", lambda: ship(order_id))
```

## Worker integration

`build_task_context` (`worker.py:173`): pick variant by coroutine-ness of `task.func`.
Both still build the Django `task_result` as today (for `.task_result`/`.attempt`) and
wrap the live Absurd ctx. Sync variant also captures the worker loop
(`asyncio.get_running_loop()`, available — `build_task_context` runs in the handler
coroutine on the loop) for bridging.

**Async variant:** methods delegate straight — `await absurd_ctx.step(name, fn)` etc.

**Sync variant (bridge):** runs in the threadpool thread; bridges each async ctx op to
the loop via `run_coroutine_threadsafe(coro, loop).result()`. `step` uses the SDK's
public `begin_step`/`complete_step` (not the bundled `step`) so the user's `fn` runs
**in the executor thread, not on the loop** (no loop-block):

1. `handle = bridge(absurd_ctx.begin_step(name))` — async DB lookup.
2. `handle.done` → return `handle.state`.
3. else `rv = fn()` — sync, this thread.
4. `bridge(absurd_ctx.complete_step(handle, rv))` — async persist; return `rv`.

`run_step` = the sync decorator sugar over `step` (mirrors SDK: run at decoration,
rebind name to result). `sleep_for`/`sleep_until`/`heartbeat` bridge their async ctx op;
sleep's coroutine raises `SuspendTask`, which `.result()` re-raises into this thread →
propagates out. Bridge happy-path is deadlock-free (the loop is idle awaiting
`run_in_executor`, so it services the bridged coroutine; the shared `AsyncConnection` is
touched only from the loop — same invariant `drain_queue`'s `gather` already relies on).
Plan detail: pass a generous timeout (or document accept) so a mid-op worker shutdown
can't hang the thread.

**Logging fix — the whole control-flow trio (I1)** (`build_handler`, `worker.py:236`):
`SuspendTask`/`CancelledTask`/`FailedTask` all subclass `Exception` and are treated as
control flow by SDK dispatch (`except (SuspendTask, CancelledTask, FailedTask): pass`,
`__init__.py:2302`). Pre-feature the handler never saw them (no ctx access); now any ctx
DB op can raise `Cancelled`/`Failed` (sqlstate AB001/AB002 via
`_task_state_exception_handling`). Add arms BEFORE `except Exception`, each re-raising
so SDK dispatch handles them:

```python
except SuspendTask:
    logger.info("django-absurd task suspended: name=%s task_id=%s ...")
    raise
except CancelledTask:
    logger.info("django-absurd task cancelled: name=%s task_id=%s ...")
    raise
except FailedTask:
    logger.warning("django-absurd task failed (explicit): name=%s task_id=%s ...")
    raise
except Exception:
    logger.exception("django-absurd task failed: ...")
    raise
```

Suspend/cancel = normal lifecycle, not crashes. Nothing else in the worker changes.

## Admin — steps under the task

Steps ARE checkpoints (persisted rows). `checkpoints` admin entity already carries
`task_id`, `checkpoint_name` (= step name), `state` (jsonb = cached return value),
`owner_run_id`, `status`; sleep writes a checkpoint too (wake time as ISO state —
verified `sleep` = `_persist_checkpoint` + `schedule_run`, never a `w_`/waits row).
Today it's a standalone changelist only — Task detail inlines Runs only.

**Checkpoints inline (new):** read-only, on Task detail, mirroring `ReadOnlyRunInline` +
`build_run_inline`. Shows each step (name + cached state + status) and the sleep
checkpoint (wake time). Prerequisite — give the synthetic `Checkpoint` model a `task`
relation exactly as runs do (`admin_views.py:267-277`: FK `task`, `to_field="task_id"`,
`db_column="task_id"`, `db_constraint=False`); converting the column renames the field
to `task` (attname `task_id`), so the checkpoints `EntitySpec.search_fields` `"task_id"`
→ `"task__task_id"` (runs did this), `list_display` `"task_id"` keeps working via
attname. Specify the inline's fields/ordering/`related_name` at plan time.

**Sleeping-run resume time (I2):** `schedule_run` flips the **same** run to
`state='sleeping'` + `available_at=wake_at` (not a new run) — `runs`/`sleeping` maps to
Django status `RUNNING` (`backends.py:163`). Add `available_at` to `RUN_INLINE_FIELDS`
so the existing Runs inline shows a suspended task + when it resumes. (No Waits inline
here — `waits` rows come from `await_event`, deferred to the Events spec.)

## Docs (replay education = first-class)

- **AGENTS.md** — new "Durable steps & sleep" section: both contexts, sync + async
  examples, and the full footgun set:
  - **effectively-once** — steps persist after `fn` returns on a separate connection;
    executed at-least-once in a crash window; keep side effects idempotent where it
    matters.
  - **deterministic naming/order** — `_get_checkpoint_name` dedups by per-run call order
    (`name`, `name#2`, …); branching/loops that change the number or order of same-named
    `step`/`sleep` calls between runs bind cached values to the wrong sites. `step` and
    `sleep` share one checkpoint namespace/counter.
  - **JSON-serializable step returns** — persisted via `json.dumps`; a model/`datetime`/
    `Decimal` raises at persist (after the side effect); `tuple` → `list` on replay
    (type asymmetry: run 1 returns the live object, resume returns the JSON round-trip).
  - **never swallow `SuspendTask`** — by the time it raises, the run is already
    `sleeping`; a broad `except Exception` that eats it → `complete_run` on a sleeping
    run → failure. Re-raise `SuspendTask`/`CancelledTask`.
  - **long steps vs `claim_timeout`** — a step must finish within `claim_timeout`
    (default 120s) or call `ctx.heartbeat()`, else the lease expires and the run runs
    again concurrently.
  - **absurd-only** — a `ctx.step` task is absurd-backend-only; under Django's
    immediate/other backends `takes_context` yields a plain `TaskContext` →
    `AttributeError`.
  - sleep resume re-claims the **same** run — attempt does not increment.
- **docs/web** — new "Durable workflows" page + `zensical.toml` nav entry; mirror
  AGENTS.md; build clean (`uvx zensical build`).
- **README** — one link, no growth.
- **Example** — extend `examples/web` with a durable workflow task + view (the "wait
  task": a button deferring an action ~5s via `sleep_for`, plus a `step`). Reuses the
  compose stack; re-run to confirm.

## Testing

Behavioral, through real entrypoints (enqueue + `run_worker` burst), real DB, no mocks,
no monkeypatch. Durable suspend reschedules + re-runs →
`@pytest.mark.django_db(transaction=True)`. Parametrize sync `def` (bridge) and
`async def` (direct) through the same scenarios.

**Timing recipe (I6 — pins the flake window):** wake needs real wall-clock — the SDK
compares Python `datetime.now(utc)` while claim uses DB `clock_timestamp()`, and
`absurd.fake_now` is DB-side only (Python-side monkeypatch banned). So: `sleep_for` a
duration long enough (~0.5–1s) that `drain_queue`'s claim loop empties and returns
**before** wake; assert interim state; then real `time.sleep` past wake; then a second
`drain_queue` resumes → completes.

- **step caches across replay** — task: `step` (bumps a module counter) → `sleep_for` →
  completes. After drain 1 (suspended) then drain 2 (resumed): counter incremented
  **once** (cached), non-step body ran on both passes.
- **sleep suspends then resumes** — after drain 1, `get_result` status is `RUNNING`
  (sleeping maps to RUNNING; a distinct "suspended" status is not observable); after
  drain 2, completes with the right result. Wording avoids "new run/attempt" — resume
  re-claims the same run, attempt stays 1.
- **trio logged as lifecycle, not crash** — `caplog` on `django_absurd`: suspend →
  "suspended"; a genuine error still → "failed".
- **admin** — HTTP-test the Task detail: log in, GET the task change page; assert the
  step (checkpoint_name + cached state) renders in the Checkpoints inline; assert the
  sleeping run's `available_at` renders in the Runs inline.
- **typing** — mypy-in-CI covers the exported contexts; add a typed usage snippet.

## Out of scope (own future specs)

Events pillar (`await_event` + app-side `emit_event`, + the Waits admin inline);
`await_task_result` (cross-queue child + deadlock guard); sync-worker mode.

## AMENDMENT (post-review): accessor-based exposure — respect Django's contract

Original design subclassed Django `TaskContext` + handed it as the `takes_context` arg.
Rejected in final review: substituting a narrower subtype violates Django's typed
contract (`@task` promises the handler a base `TaskContext`) — forces a permanent
`[arg-type]` on every typed handler + a `[misc]` on the frozen subclass, and conflates
Django's result-context with Absurd's runtime. **Replaced with an accessor**; no
`TaskContext` subclass.

**New shape.** The Absurd SDK stashes the live ctx in a contextvar the worker sets at
dispatch, exposed by the SDK's PUBLIC `get_current_context()` (in its `__all__`).
Durable access is a pair of django-absurd accessors — `get_absurd_context()` (sync
tasks) / `aget_absurd_context()` (async tasks) — orthogonal to the (now plain,
unmodified) Django `TaskContext`:

```python
from django_absurd import aget_absurd_context, get_absurd_context

@task                                  # or takes_context=True for a plain .task_result
async def workflow(order_id: int) -> None:
    await aget_absurd_context().step("charge", charge)
```

- **Async task → pure delegation, no wrapper.** `aget_absurd_context()` returns the
  SDK's own `AsyncTaskContext` (`get_current_context()`); user calls
  `await context.step(...)` on Absurd's own py.typed object. We define no class, mirror
  no signatures — and get the future primitives (events, `await_task_result`) for free.
  No `run_step` (SDK omits it on async).
- **Sync task → thin bridge wrapper (unavoidable).** Worker is `AsyncAbsurd`, so the
  live ctx is always `AsyncTaskContext` (async methods); a sync `def` can't `await`, and
  the SDK's sync `TaskContext` needs its own sync connection we don't have. So
  `get_absurd_context()` returns a small wrapper mirroring the SDK sync signatures 1:1,
  bridging each call to the async ctx via `run_coroutine_threadsafe(coro, loop)` (using
  the stashed worker loop). Keeps `run_step` (all three forms) — the sync-only asymmetry
  matches the SDK.
- **Two concrete-typed accessors, no auto-select, no user `t.cast`.** The user picks the
  accessor by task kind (matching the codebase's sync / `aget_result` async convention).
  Each returns ONE concrete type — `get_absurd_context() -> AbsurdTaskContext`,
  `aget_absurd_context() -> AsyncTaskContext` — so typed call sites need no cast.
  Outside a task → `get_current_context()` is `None` → each raises a clear error naming
  itself.

**Worker.** `build_task_context` no longer injects our type — `takes_context` tasks get
a plain Django `TaskContext` (built as today for `.task_result`/`.attempt`). Sync-task
context propagation into the executor thread via **`asyncio.to_thread`** (copies the
contextvar per call — so `get_current_context()` resolves in-thread) + a stashed worker
loop for the bridge. Thread-safety unchanged: all DB ops still run on the loop (one
shared `AsyncConnection`, serialized); contextvars isolate per asyncio-Task + per
to_thread copy; a missing propagation fails loud (`None`), never silently races.

**Gone:** `TaskContext` subclass, `[misc]`, `[arg-type]`, the Django-contract violation.
**Kept identical:** step/sleep/heartbeat/headers/run_step behavior, effectively-once
semantics, admin (Checkpoints inline + `available_at` — untouched by this pivot), docs
footgun set. **Tests:** behavioral assertions unchanged (they validate the refactor);
only the test-task call sites re-point to the two accessors + drop narrowed
annotations/casts; add an outside-a-task guard test for BOTH accessors.
