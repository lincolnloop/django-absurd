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
- `begin_step` / `complete_step` are mirrored too — see below, they become the single
  home for step logging.

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

`step` is the interesting case. It cannot be shared as a method — the sync flavour must
run the user's `fn` in the executor thread between `begin_step` and `complete_step`, so
one is a coroutine function and one is not. But its _logging_ can be shared, by putting
the lines in `begin_step` and `complete_step`:

- `begin_step` knows `handle.done`, which IS the replay signal.
- `complete_step` is where a step finished.

Both flavours' `step()` call those, so neither carries logging of its own. A caller
driving `begin_step`/`complete_step` manually gets the same lines free.

`step completed` still carries a duration. `StepHandle` exposes `checkpoint_name`, a
unique slot id (the SDK numbers repeated step names itself), so `begin_step` stashes a
monotonic start under that key and `complete_step` pops it. A dict field on the wrapper
holds them; the wrapper is a frozen slots dataclass, which blocks rebinding the
attribute but not mutating the dict.

Edge, degraded not defended: `get_absurd_context()` builds a fresh wrapper per call, so
a caller driving `begin_step` and `complete_step` from two separate accessor calls has
no stashed start. That logs the line without a duration.

## Events

All at INFO, on `django_absurd.context`. One level, nothing to reason about, and visible
by default since the console handler sits at INFO. A project with many steps quiets that
one child logger without losing worker or run lines.

| Event                | Where                | When                                    |
| -------------------- | -------------------- | --------------------------------------- |
| `step replayed: …`   | `begin_step`         | `handle.done` — work skipped            |
| `step completed: …`  | `complete_step`      | step finished — with duration           |
| `sleep suspended: …` | `sleep_for`/`_until` | BEFORE the await — it will not return   |
| `sleep resumed: …`   | `sleep_for`/`_until` | the call returned; checkpoint satisfied |
| `event awaiting: …`  | `await_event`        | BEFORE the await                        |
| `event received: …`  | `await_event`        | the call returned                       |
| `event emitted: …`   | `emit_event`         | after                                   |
| `awaiting result: …` | `await_task_result`  | before                                  |

Suspending calls break the log-past-tense convention deliberately: `sleep_for` raises
`SuspendTask` to unwind the run, so nothing after it executes. Only a line before the
await can report the suspension; the resume line is the same call returning on a later
attempt.

`sleep suspended` overlaps the shipped run-level `task suspended` — two INFO lines per
suspension. Not redundant: the run line says the run stopped and for how long, the sleep
line says which step and until when.

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
