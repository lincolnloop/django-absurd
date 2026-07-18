# Durable Steps + Sleep — Design

**Goal:** expose Absurd's durable-execution primitives **Steps** + **Sleep** to
django-absurd task functions. A `takes_context=True` task can checkpoint work (`step` —
run once, persist result, replay from cache) and durably suspend
(`sleep_for`/`sleep_until` — release the worker, resume later). Turns django-absurd from
at-least-once fire-and-forget into a workflow engine.

Grounded in Absurd docs:
[Concepts → Steps](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints),
[Concepts → Sleep](https://earendil-works.github.io/absurd/concepts/),
[Python SDK](https://earendil-works.github.io/absurd/sdks/python/).

## Scope

IN: `step`, `sleep_for`, `sleep_until`; fix `SuspendTask` mislogging; admin visibility
(steps + pending suspensions under the task); replay-semantics docs + a runnable
example.

OUT (own future specs): **Events** pillar (`await_event` in-task + `app.emit_event`
app-side emit surface); `await_task_result` (cross-queue child + same-queue-deadlock
guard); `heartbeat`; sync-worker mode.

## Replay semantics (the load-bearing contract)

Absurd durable execution: on every retry/resume the **whole task body re-runs
top-to-bottom**. Completed `step(name, ...)` calls return their cached value instead of
re-executing; a `step` never runs twice across restarts/retries/resumes. Sleep
suspends + schedules a future run; on wake the same replay applies (checkpoints skip,
sleep checkpoint records wake time so it doesn't re-sleep).

Consequence (Absurd docs, verbatim): _"Code outside steps may execute multiple times
across retries. Keep side-effects inside steps."_ This footgun is a first-class
deliverable — must be taught loudly (see Docs), else users write double-charging tasks.

## Context exposure — extend Django `TaskContext`, match ctx to task kind

Today worker hands `takes_context=True` tasks a plain Django `TaskContext`
(`task_result` only); Absurd SDK `ctx` withheld (`worker.py:216`). Change: hand a
django-absurd context that **subclasses Django `TaskContext`** (keeps
`.task_result`/`.attempt`) AND adds `.step`/`.sleep_for`/`.sleep_until` (delegating to
the live Absurd ctx).

Two variants, chosen by `inspect.iscoroutinefunction(task.func)` — mirrors SDK's own
`TaskContext` (sync) vs `AsyncTaskContext` (async) split. Absurd's Python docs are
sync-first, so sync tasks must feel native (no `await`):

```python
# sync variant — Absurd's primary Python style, no await
class DurableContext(TaskContext[...]):
    def step(self, name: str, fn: Callable[[], R]) -> R: ...
    def sleep_for(self, name: str, seconds: float) -> None: ...
    def sleep_until(self, name: str, when: datetime | int | float) -> None: ...

# async variant
class AsyncDurableContext(TaskContext[...]):
    async def step(self, name: str, fn: Callable[[], Awaitable[R]]) -> R: ...
    async def sleep_for(self, name: str, seconds: float) -> None: ...
    async def sleep_until(self, name: str, when: datetime | int | float) -> None: ...
```

Names/args mirror the SDK 1:1 — no invented API, Absurd docs transfer verbatim. Both
strict-typed + publicly exported so `ctx: DurableContext` yields autocomplete + mypy
(incl. `step` generic return `R`). Names provisional (`DurableContext` /
`AsyncDurableContext`) — settle at plan time.

Usage:

```python
@task(takes_context=True)
def workflow(ctx, order_id):
    charge = ctx.step("charge", lambda: charge_card(order_id))  # once; cached on replay
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

`sleep_for`/`sleep_until`: bridge `absurd_ctx.sleep_until(...)`; coroutine raises
`SuspendTask`, `.result()` re-raises into this thread → propagates out.

**`SuspendTask` logging fix** (`build_handler`, `worker.py:236`): `SuspendTask`
subclasses `Exception`, so today it hits `except Exception` and is mislogged "task
failed" (functionally suspend still works — SDK dispatch catches it, checkpoint already
persisted + run rescheduled). Add earlier arm:

```python
except SuspendTask:
    logger.info("django-absurd task suspended: name=%s task_id=%s attempt=%d ...")
    raise
except Exception:
    logger.exception("django-absurd task failed: ...")
    raise
```

Suspend = normal lifecycle event, not failure. Nothing else in the worker changes.

## Admin — steps under the task

Steps ARE checkpoints (persisted rows). `checkpoints` admin entity already carries
`task_id`, `checkpoint_name` (= step name), `state` (jsonb = cached return value),
`owner_run_id`, `status`; sleep writes a checkpoint too (wake time as state). Today it's
a standalone changelist only — Task detail inlines Runs only.

Add a read-only **Checkpoints inline** to Task detail, mirroring `ReadOnlyRunInline` +
`build_run_inline` (`fk_name="task"`, read-only perms, `show_change_link`): shows each
step (name + cached state + status) and the sleep checkpoint (wake time) under the task.

Prerequisite: give the synthetic `Checkpoint` admin model a `task` relation (join on
`task_id`) so the inline binds — runs already have this; checkpoints currently expose
only a `task_id` column.

**Sleep-suspended state** is already visible via the existing **Runs inline** (the
rescheduled future run shows the task is asleep + when it resumes) — no new surface
needed. **No Waits inline here:** the `waits` entity (columns `event_name`/`timeout_at`)
is populated by `await_event`, not by sleep — sleep only writes a checkpoint + calls
`absurd.schedule_run`. The Waits inline belongs with the Events spec, where
`await_event` actually fills it.

## Docs (replay education = first-class)

- **AGENTS.md** — new "Durable steps & sleep" section: both contexts, sync + async
  examples, loud replay/"side-effects inside steps" callout.
- **docs/web** — new "Durable workflows" page + `zensical.toml` nav entry; mirror
  AGENTS.md; build clean (`uvx zensical build`).
- **README** — one link, no growth.
- **Example** — extend `examples/web` with a durable workflow task + view (the "wait
  task": a button deferring an action ~5s via `sleep_for`, plus a `step`). Reuses the
  compose stack; re-run to confirm.

## Testing

Behavioral, through real entrypoints (enqueue + `run_worker` burst), real DB, no mocks.
Durable suspend reschedules + re-runs → `@pytest.mark.django_db(transaction=True)`.
Parametrize sync `def` (bridge) and `async def` (direct) through the same scenarios.

- **step caches across replay** — task: `step` (bumps module counter) → `sleep_for` tiny
  duration → completes. Drain 1 suspends; after wake instant, drain 2 resumes → step
  counter incremented **once** (cached), non-step body ran both attempts.
- **sleep suspends then resumes** — not complete after drain 1 (rescheduled run
  pending); completes with right result after drain 2 (`get_result`).
- **SuspendTask logged "suspended", not "failed"** — `caplog` on `django_absurd`.
- **admin** — HTTP-test the Task detail: log in, GET the task change page, assert the
  step (checkpoint_name + cached state) renders in the Checkpoints inline; assert the
  rescheduled run shows in the Runs inline while suspended.
- **typing** — mypy-in-CI covers exported contexts; add a typed usage snippet.

## Out of scope (own future specs)

Events pillar (`await_event` + app-side `emit_event`, + the **Waits admin inline** it
populates); `await_task_result` (cross-queue child + deadlock guard); `heartbeat`;
sync-worker mode. Async worker + bridge already cover both task kinds.
