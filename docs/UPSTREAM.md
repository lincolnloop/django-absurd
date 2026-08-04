# Upstream asks

Gaps in [Absurd](https://github.com/earendil-works/absurd) — SDK or schema — that
django-absurd currently compensates for. One section per **askable change**: what we
would want, why, and which workaround it retires. Grouped that way on purpose, so a
section can be filed as-is rather than re-derived from a list of symptoms.

**Nothing here is filed yet.** When upstream ships one, delete the section rather than
marking it done, and remove the workaround it names.

Three of these are marked in code by the `noqa: SLF001` comments in
`django_absurd/worker.py` — each names the private attribute it reaches and why.

## Accept a future availability on `spawn`

`spawn` can only make a task immediately available; the schema's `spawn_task` takes no
availability argument.

Needed for `run_after`. Today a deferred enqueue spawns a **wrapper** task that sleeps
until due and then enqueues the caller's task (`django_absurd/deferred.py`). The obvious
alternative — claim the caller's own task and sleep inside it — starts both cancellation
clocks before any of the work exists, which is the design we built and abandoned (see
[WHY.md](WHY.md), "deferred enqueue").

So this ask and the next are entangled: fix the clocks and an injected sleep becomes
viable; fix `spawn` and no sleep is needed at all.

Retires: the wrapper name, its handler, and the derived idempotency key that keeps the
wrapper from deduping against itself.

## Exclude deliberate suspension from the cancellation clocks

Both cancellation policies treat "suspended on purpose" and "waiting to be picked up" as
the same thing. Durable sleep is a first-class Absurd feature the policy has no concept
of.

- `max_duration` is measured from `first_started_at` and keeps accruing across
  `ctx.sleep_for` / `await_event`. A workflow that deliberately waits a day cannot also
  be given a one-hour execution budget.
- `max_delay` cancels a task that has not started within its window, and the check stops
  applying once a run is claimed. A task claimed promptly and then suspended
  indefinitely is never cancelled by delay.

One ask, two symptoms: the clocks should measure work, not waiting.

Retires: nothing. No workaround exists — we document the trap. It is also what makes the
ask above expensive.

## Surface a run's terminal outcome to the worker

**Concretely: a third entry in `AbsurdHooks`,** `after_run_outcome(ctx, outcome)`, fired
once the run's completion / failure / cancellation is persisted. That TypedDict already
exists and its own docstring names "tracing, **logging**, and context propagation" as
its purpose — this is an increment to a documented extension point, not a new concept.

Its two current hooks both stop short. `before_spawn` runs at enqueue;
`wrap_task_execution` wraps the handler call, so it is still inside the window _before_
the outcome is written. Verified against upstream `main`, not just our pinned wheel:
there is no `after_task` / `on_complete` / `after_run`, and no hook receives the result
or the exception outside the wrapped handler.

Meanwhile `_execute_task` catches `SuspendTask` / `CancelledTask` / `FailedTask` and
swallows the result of its own `fail_run` call, so nothing reports what the run became.

Two consequences:

- When attempts remain but `max_duration` is exceeded, `fail_run` cancels the task
  instead of retrying it, and we never learn the task went terminal.
- There is no post-persist seam at all on the blocking-worker path, which is the SDK's
  own claim → execute → gather loop.

Needed wherever a worker must know how a run actually ended. Burst draining reads the
outcome back from the database for exactly this reason (`fetch_run_outcome`), and any
observer of an Absurd-side ending — a cancellation, an expired claim, a `max_duration`
sweep — has no seam to learn of it from.

## Public API to execute one claimed run and return its outcome

`work_batch` runs its own claim loop. Burst draining needs "execute exactly these
claims, then stop", so `execute_claimed_run` calls `client._execute_task`. And because
no public accessor keys by `run_id` — only by `task_id`, which collapses a retry's
several runs into one answer — the outcome comes from a second read through the
per-queue dynamic model.

Ask: a public `execute(claimed) -> outcome`. It retires both workarounds at once; a read
accessor keyed by `run_id` would retire half.

Retires: the `_execute_task` reach, `fetch_run_outcome`, and the `sync_to_async` hop
that read forces — along with the once-per-run `close_old_connections` it needs so a
Django session on asgiref's thread-sensitive executor cannot outlive the worker and
block `DROP DATABASE`.

## Call an optional resolver before deferring an unknown task

A framework integration cannot enumerate its tasks. Django has no task registry — tasks
are decorated functions, discoverable only by importing the module that defines them —
so eager registration means Celery-style `tasks.py` autodiscovery, which misses every
task defined anywhere else. What is needed is resolution by dotted path, on demand.

Ask: at the existing `if not registration:` branch in `_execute_task`, call an optional
resolver callable before falling through to the unknown-task defer. Registration stays
exactly as it is for everyone who can enumerate.

django-absurd resolves by replacing `_registry` with `LazyTaskRegistry`, a `dict`
subclass overriding `.get`, so any importable `Task` resolves on first claim. That works
because all three registry reads (SDK lines 1252, 1728, 2233) go through `.get`. If one
ever becomes `_registry[name]` or `name in _registry`, the override stops firing and the
SDK defers the run with jitter instead — **silently**, no exception, no log, task never
executes. Cheap workaround, bad failure mode.

Retires: `LazyTaskRegistry`.

## Expose `run_step` on `AsyncTaskContext`

The SDK's sync `TaskContext` has `run_step` — a decorator convenience over `step`,
deriving a name from `fn` when none is given. `AsyncTaskContext` has only `step`; no
async equivalent exists.

Our sync `AbsurdTaskContext.run_step` bridges the SDK's real sync method.
`AsyncAbsurdTaskContext` cannot offer one without inventing surface the SDK itself
lacks, so it doesn't — documented in `AGENTS.md`'s API reference table as a sync-only
row.

Retires: nothing. No workaround exists; the async wrapper's surface stays one method
short of the sync one until the SDK adds it.

## Expose `attempt`, `run_id` and `enqueue_at` on the task context

`ClaimedTask` carries `attempt` and `run_id`; `TaskContext` exposes neither, so
`read_sdk_claimed_task` pulls the claimed row out of `ctx._task`, and every caller
indexes it from there. Every attempt-aware behaviour depends on that reach —
retry-terminal predicates, `TaskResult.worker_ids`, `TaskContext.attempt`.

Nothing exposes the task's enqueue time either, so a `TaskResult` a worker builds
carries `enqueued_at=None` — correct at enqueue, unknown at execution.

Ask: public properties for all three.

Retires: `read_sdk_claimed_task`, and the `enqueued_at=None` compromise.
