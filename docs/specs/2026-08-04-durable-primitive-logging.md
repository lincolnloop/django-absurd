# Durable-primitive logging

Follow-up unit to [#25](https://github.com/lincolnloop/django-absurd/issues/25). Units 1
(Django's task signals, #143) and 2 (the package's own loggers, #146) shipped. This unit
covers what neither reached: steps, step replays, durable sleeps, event waits, emits.

## What is missing today

Run granularity is visible — `task started`, `task suspended`, `task completed` come
from the `wrap_task_execution` hook. So a sleeping run says it suspended, and says it
resumed.

Invisible: **which** step, **how long**, **which event**, and above all **step replay**
— a checkpointed step skipped on a later attempt. Replay is the most confusing thing
about durable execution, and nothing reports it. `django_absurd/context.py` has no
logging at all.

## Why hooks cannot do it

The two SDK hooks (`before_spawn`, `wrap_task_execution`) sit around a whole run. Steps
and sleeps are calls on the task **context**, which the hooks never see.

Subclassing the context is not available either. The SDK builds contexts with
`object.__new__(AsyncTaskContext)` inside private `_create_async_task_context`, called
from private `_execute_task`; `AsyncTaskContext.__init__` raises
`TypeError("Cannot create AsyncTaskContext instances")`. No `context_class` argument, no
registry, no per-primitive hook. Monkeypatching the factory is rejected — fragile
against every SDK bump.

So: **composition**. Wrap the context, log in the wrapper. Precedent already in the tree
— `AbsurdTaskContext` (`context.py`) wraps the async context for sync tasks.

## Contract: mirror the SDK, including its asymmetries

Our wrappers mirror the SDK's context surface **per flavour**. Method names and
signatures follow the SDK. Where the SDK is itself asymmetric, so are we, and the
asymmetry is recorded rather than papered over:

- `run_step` exists on the SDK's **sync** `TaskContext` only; `AsyncTaskContext` has
  none. So our async wrapper gets no `run_step`. Inventing one would add surface the SDK
  lacks and produce awkward ergonomics (a decorator returning a coroutine). Goes to
  `docs/UPSTREAM.md` as an ask.
- `await_task_result` is on **both** SDK contexts, and our sync bridge lacks it. Gap on
  our side: add it. Alpha, so no compatibility constraint.
- `begin_step` / `complete_step` are mirrored too; `begin_step` is where the replay line
  lives, since `handle.done` is the signal.

`.absurd_ctx` stays the escape hatch on both, for anything unmirrored.

## Structure: async owns it, sync inherits by delegation

One implementation, two entry points — the standing rule for this package.

`AsyncAbsurdTaskContext` (new, `context.py`) holds the log calls. The existing sync
`AbsurdTaskContext` stops delegating to the raw SDK context and delegates to the async
wrapper instead, running its coroutines on the worker loop exactly as it does today:

```python
# sync bridge, before → after: same shape, different target
self.run_on_loop(self.absurd_ctx.sleep_for(step_name, duration))
self.run_on_loop(self.async_ctx.sleep_for(step_name, duration))
```

Every delegating primitive then has exactly one implementation and one log line.

`step` is the one exception. It cannot be shared as a method — the sync flavour must run
the user's `fn` in the executor thread between `begin_step` and `complete_step`, so one
is a coroutine function and one is not. Its logging splits by what each place knows:

- **`begin_step` logs `step replayed`.** It knows `handle.done`, needs no timing, and
  lives in one place — so a caller driving the primitives manually gets the replay line
  too.
- **`step()` logs `step completed`.** It has the start and the end as locals, so the
  duration needs no plumbing. Two call sites, sync and async, sharing one message
  builder.

That leaves one duplicated call rather than a timing channel between two methods.
Passing the start time along — a dict on the wrapper, or a `StepHandle` subclass
carrying it — was machinery for a case that does not arise, since `step()` always begins
and completes on the same instance.

## Events

All at INFO, on `django_absurd.context`. One level, nothing to reason about, and visible
by default since the console handler sits at INFO. A project with many steps quiets that
one child logger without losing worker or run lines.

| Event                | Where                | When                                        |
| -------------------- | -------------------- | ------------------------------------------- |
| `step replayed: …`   | `begin_step`         | `handle.done` — work skipped                |
| `step completed: …`  | `step`               | step finished — with duration               |
| `sleep suspended: …` | `sleep_for`/`_until` | the call raised `SuspendTask`               |
| `sleep resumed: …`   | `sleep_for`/`_until` | the call returned — checkpoint satisfied    |
| `event awaiting: …`  | `await_event`        | the call raised `SuspendTask`               |
| `event received: …`  | `await_event`        | the call returned, with the payload's event |
| `event emitted: …`   | `emit_event`         | after                                       |
| `awaiting result: …` | `await_task_result`  | after                                       |

**Classify a suspension by its exception, never by logging before the await.** Probed:
on resume the task body re-runs **from the top**, and the suspending call returns
immediately from its checkpoint. A line before the await therefore fires on every
attempt and claims a suspension that is not happening. `sleep_for` and `await_event`
both raise `SuspendTask` while suspending and return normally on resume, so:

```python
try:
    await ...          # the SDK call
except SuspendTask:
    # "sleep suspended" / "event awaiting"
    raise
# "sleep resumed" / "event received"
```

Both lines are then true whenever they appear.

`sleep suspended` overlaps the shipped run-level `task suspended` — two INFO lines per
suspension. Not redundant: the run line says the run stopped and for how long, the sleep
line says which step and until when.

Step lines report `checkpoint_name`, not the bare name: the SDK numbers repeated step
names itself, so two `step("dup")` calls in one run become `dup` and `dup#2`. The bare
name would make them indistinguishable in the log.

Plain text, ASCII in our own literals — same discipline as the shipped loggers. A
caller's own strings (step names, event names) travel verbatim.

## Testing

Through real tasks with the `dj_absurd` fixture, asserting full rendered messages.

- **Replay** needs a task that completes a step, then fails, then retries:
  `step completed` on attempt 1, `step replayed` on attempt 2.
- **Suspension** needs the `sleep_a_week` shape plus `frozen_time.shift`.
- **Every event driven from both a sync and an async task** — the point of the design is
  that both share one implementation, so both paths must be proven, not assumed.
- The wrapper's mirrored surface is exercised, not asserted structurally: a test that
  reads `__dict__` or compares method lists proves nothing about behaviour.

## Not in this unit

- Instrumenting the SDK's internal checkpointing (the `begin_step` inside `sleep_for`).
  Only calls user code makes are logged.
- A task reaching `absurd_sdk.get_current_context()` directly bypasses the wrapper.
  Documented, not defended.
- **The `:run_after` deferral wrapper's own primitives.** `build_deferred_handler`
  (`django_absurd/deferred.py`) receives the raw SDK context, so a deferred task's
  wait-until-due sleep and its `enqueue:<target>` step emit no lines. Deliberate: the
  run-level `task suspended` line already makes the deferral visible, and this unit logs
  the calls USER code makes. Wrapping that ctx in `AsyncAbsurdTaskContext` is a one-line
  change if per-step visibility is ever wanted.

## Probed, not assumed

Verified against a running worker before this spec was accepted:

- `begin_step` returns `done=False` on the first attempt and `done=True` with the stored
  `state` on a later one. Replay is exactly that flag.
- **The task body re-runs from the top on resume**, which is what invalidates any line
  logged before a suspending call.
- `sleep_for` and `await_event` raise `SuspendTask` while suspending and return on
  resume (`await_event` returning the emitted payload).
- Repeated step names produce `checkpoint_name` values `dup`, `dup#2`.
- A hand-rolled wrapper stands in for the context from a real async task, driving
  `step`/`emit_event` through it.
- `absurd_sdk.get_current_context()` returns the raw SDK context — identity-equal to
  `.absurd_ctx` — confirming the documented bypass.
- `await_task_result` is present on the SDK's async context and absent from our sync
  bridge.
- Our sync `step()` already replays correctly: across five attempts the body ran
  **once** and the checkpointed value came back four more times, entirely unlogged. That
  is the feature this unit makes visible.
