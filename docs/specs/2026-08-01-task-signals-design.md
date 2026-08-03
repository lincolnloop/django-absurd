# Send Django's task signals from AbsurdBackend

Unit 1 of [#25](https://github.com/lincolnloop/django-absurd/issues/25). Stays under #25
— no separate issue.

## Problem

`django/tasks/__init__.py:4` does `from . import checks, signals`, so the three logging
receivers in `django/tasks/signals.py` connect eagerly in any project with `TASKS`
configured. `ImmediateBackend` sends all three (`immediate.py:29,37,69,73`);
`DummyBackend` sends `task_enqueued` only (`dummy.py:24`) — it never executes.
`AbsurdBackend` sends **none**.

Consequences: Django logs no task lifecycle at all against our backend, and a user's
`@receiver(task_finished)` silently never fires. Contract defect, independent of the
styled-logger work #25 is really about.

Non-goals here: Absurd's own vocabulary (steps, sleeps, events, beat), logger hierarchy
rename, pretty formatter, support gate. Later units.

## Never register a receiver

django-absurd **sends** signals, never listens. An unfiltered receiver fires for every
backend, so we would report tasks that never touched Absurd. If one is ever needed:
`sender=AbsurdBackend` (senders are the backend _class_, per Django's `type(self)`).

## Seam 1 — enqueue

`backends.py:enqueue`. Build the `TaskResult` before returning; send
`task_enqueued.send(type(self), task_result=result)`. Sent after a successful spawn,
never before.

`await task.aenqueue()` reaches the identical code through
`sync_to_async(self.enqueue, thread_sensitive=True)` (`base.py:89-93`), which we do not
override — `AbsurdTask.aenqueue` only adds the inert-params warning (`tasks.py:68-72`).
So one send covers both entry points; no async twin, unusually for this project.

Neither path lands a receiver on a thread with a running loop — but because Django
already hopped it, not because the code is inherently sync. From an async caller the
receiver runs on asgiref's shared thread-sensitive thread, which is Django's choice, not
ours. So this is the one seam where a receiver can reach the ORM at all; it is asserted
in tests rather than left to this paragraph, and it implies nothing about the worker
sends.

## Seam 2 — worker

`worker.py:build_handler`. Single dispatch point for sync and async tasks — async
awaited directly, sync via `asyncio.to_thread` — and every SDK dispatch path
(`work_batch`, `start_worker`, our `execute_claimed_run`) funnels through
`_registry.get` into it. So async parity is structural, not duplicated. The worker-side
`TaskResult`, today built only when `task.takes_context` (`build_task_context`,
`worker.py:311`), becomes unconditional; `TaskContext` wraps the same object.

**The worker's own backend and queue are plumbed into `build_handler`; the re-imported
task is not trusted for either.** `LazyTaskRegistry.get` re-imports the decorator-time
task (`worker.py:114`), so `task.backend` and `task.queue_name` are its _definition_
values. A task defined on a non-Absurd backend and enqueued via `.using(backend=...)` —
a path `backends.py:127-134` explicitly supports — would otherwise send `task_enqueued`
with sender `AbsurdBackend` and then started/finished with sender `ImmediateBackend` and
`backend="default"`: a `sender=AbsurdBackend` receiver sees an enqueue with no start or
finish, and an Immediate-filtered receiver gets ghost events for tasks it never ran.

So the resolved backend (already in hand at `arun_worker`, `worker.py:168`) and the
worker's queue (`LazyTaskRegistry.queue` / `registration["queue"]`, `worker.py:88-126`)
reach `build_handler`. Sender is `type(backend)`, so a subclass of `AbsurdBackend` is
its own sender; `TaskResult.backend` is that backend's alias; and the task object is
rebound with `.using(queue_name=..., backend=...)` when either disagrees. The alias
travels with the queue because `Task.using` is a bare `dataclasses.replace` and
`__post_init__` re-validates against whichever alias the task's DEFINITION named —
rebinding the queue alone raises `InvalidTaskBackend` when that alias is unconfigured.
`build_task_result` carries the same tuple rebind, for the same reason, on the read
path.

No DB reads for our own fields. Every one comes from the claim, our own timings, the
live exception.

| Event                             | Status       | Fields                                                                                        |
| --------------------------------- | ------------ | --------------------------------------------------------------------------------------------- |
| `task_started`, per handler entry | `RUNNING`    | `started_at`/`last_attempted_at` = now, `worker_ids=["absurd"] * attempt`, `enqueued_at=None` |
| success                           | `SUCCESSFUL` | `finished_at`, `_return_value` set                                                            |
| terminal failure                  | `FAILED`     | `finished_at`, one `TaskError` from the live exception                                        |
| non-terminal failure              | —            | **no signal**                                                                                 |
| `SuspendTask`                     | —            | **no signal** — the run is sleeping, not finished                                             |
| `CancelledTask` (AB001)           | —            | **no signal** — an ending Absurd decided, not one the task raised                             |
| `FailedTask` (AB002)              | —            | **no signal** — same                                                                          |

`_return_value` must be set on success, but not because omitting it raises:
`TaskResult.return_value` returns `_return_value`, which defaults to `None`
(`base.py:196, 202-215`). Omit it and a receiver silently reads `None` in place of the
real value — a wrong answer, not an error. It is normalized with `normalize_json`, as
Django's own backend does, so the payload agrees with what `get_result()` later reports;
the raw value still goes back to the SDK.

### Why not the SDK's hooks

`AbsurdHooks` exists for "tracing, logging, and context propagation"
(`absurd_sdk:224-229`), so `wrap_task_execution` is the obvious candidate. It buys
nothing here: it wraps the handler call on the same loop thread, still before the
outcome is persisted, and `build_handler` is our own code rather than a private reach,
so there is no access to legitimise. It would also cost two things — the direct `Task`
object (the hook sees only `task_name`) and a name-suffix filter, because the hook fires
for the `:run_after` wrapper too, which must not send started/finished.

Both hooks stay relevant elsewhere: `before_spawn` is where a correlation id would ride
into task headers, and the missing third hook is what
[the upstream ask](../UPSTREAM.md#surface-a-runs-terminal-outcome-to-the-worker) asks
for by name.

## Where receivers run

`Signal.send` calls every receiver inline on the calling thread. The worker-side sends
are inline too, in the handler's own coroutine on the worker's event loop.

**Users are not invited to build on these signals.** They are sent so Django's own
`django.tasks` logging works, and Django's three built-in receivers only log — they
never touch the ORM, so nothing on this path needs a database. A receiver of the user's
own that does reach for the ORM meets Django's `async_unsafe` guard, which raises
`SynchronousOnlyOperation` on a thread with a running loop; containment logs that as a
broken receiver. An `async def` receiver fails the same way: Django wraps it in
`async_to_sync`, which refuses to run on a thread that already has a loop.

**Receiver exceptions are contained,** through a single `send_task_signal(...)` helper
that every send goes through — worker sends and the enqueue seam alike. It catches
`Exception` (never `BaseException`: `asyncio.CancelledError` at shutdown must propagate)
and logs `logger.exception(...)` on the `django_absurd` logger. One helper, not three
inlined `except` arms, so there is one branch to cover and one raiser test to write.

Containment matters more than it looks:

- Without it a receiver's exception escapes the handler and the SDK's `except Exception`
  reads it as the _task_ failing (`absurd_sdk:2304`) — an ordinary audit receiver
  consumes attempts until every task is FAILED.
- A raiser during the success send would re-execute an already-succeeded body.
- A raiser inside the terminal-failure `except` would have `fail_run` persist the
  _receiver's_ error as `failure_reason`.
- On the enqueue seam, `enqueue_deferred_target` calls `backend.enqueue` _inside a
  checkpointed step_ (`deferred.py:63-97`). An uncontained raiser after the inner spawn
  commits leaves the checkpoint uncommitted, so the wrapper retries and enqueues the
  real task again — up to `max_attempts` duplicates from one buggy logging receiver.

So "a receiver can never damage the task" is literally true, on every seam.

**Cost, accepted:** receivers run inside the claim lease, because the run's completion
is not persisted until our handler returns. A slow receiver ages the claim toward
`claim_timeout`. That belongs to the user's receiver, not to us — document it, do not
mitigate it.

## The traceback on Django's ERROR line

Django's `log_task_finished` calls `sys.exc_info()` to attach the traceback
(`signals.py:52-53`). Sending inline, from inside the handler's `except` arm, means
Python has already populated it: the ordinary terminal-failure send needs nothing
arranged, and Django's ERROR line carries the task's own exception, matching the
`TaskError` on the payload.

The failure arm is the only send with an exception in play; `task_started` and the
success send have none.

## Absurd's own control-flow states send nothing

`SuspendTask`, `CancelledTask` and `FailedTask` are Absurd's internal control-flow
signals, raised from a run's next durable write rather than from the task's code. None
of them sends `task_finished`.

`SuspendTask` is uncontroversial — the run is sleeping, not finished, and it re-enters
the handler later.

The other two were tried and dropped. Reporting them meant a cancelled task produced a
Django `FAILED` line **only when the cancel happened to land while a worker was
mid-run** — the same cancel arriving a moment earlier, before the claim, is one of the
endings below that reports nothing. Logging that depends on a race is worse than logging
that does not happen, and the two-plus-a-list-of-exceptions shape collapses to one rule
without them:

**Django's task signals report the lifecycle Django knows — enqueued, started, and
finished on success or on a failure the task's own code raised. Endings Absurd decides
on its own are outside what Django's task signals describe.**

The handler's `except (SuspendTask, CancelledTask, FailedTask)` arm therefore only logs
on the `django_absurd` logger and re-raises. It cannot be deleted: those three are
`Exception` subclasses, so without the arm they fall into `except Exception`, which
reports an Absurd-decided ending as the task failing.

## Terminal predicate

`max_attempts is not None and attempt >= max_attempts`, both off `ctx._task`
(`ClaimedTask`, `absurd_sdk/__init__.py:158,161`).

Mirrors the pinned SQL exactly. `fail_run` retries when
`v_max_attempts is null or v_next_attempt <= v_max_attempts` (schema line 1188, with
`v_next_attempt := v_attempt + 1`), and `max_attempts integer` is nullable with no
default (line 154) — **NULL means retry forever**. So None ⇒ never terminal is the DB's
rule, not a guess.

Our `enqueue` sets it via `merged.setdefault` (`backends.py:137`) on every path, with
two exceptions that both land on the None branch safely:
`absurd_params(max_attempts=None)` survives the sentinel filter (`params.py:137-138`)
and spawns SQL NULL, and pg_cron-spawned rows never pass through `enqueue` at all.

## What task_finished promises

**It fires where the task's own code ended the run and the handler can prove that ending
is terminal.** Not a completeness guarantee — Django's wording implies one, and we do
not honour it. Absurd ends tasks on its own account, on paths that never reach a handler
and where the SDK reports no outcome (`_execute_task` swallows it,
`absurd_sdk:2302-2313`).

That is the boundary, not a gap: these signals report the lifecycle Django knows —
enqueued, started, and finished on success or on a failure the task's own code raised.
Endings Absurd decides on its own are outside what Django's task signals describe. The
list below is worth keeping because a reader will otherwise infer a promise from
Django's docs that this backend does not make.

Terminal without any `task_finished`:

- **`max_delay` cancellation** — `claim_task`'s pre-claim sweep cancels a task that has
  not started in its window. Under worker backlog: `task_enqueued`, then terminal, with
  no `task_started` either. Most likely of these in production.
- **`max_duration` while sleeping** — same pre-claim sweep. `task_started` ×N, then
  silence.
- **A concurrent cancel** during a non-terminal failure — `fail_run` raises AB001, the
  SDK swallows it, and the task is cancelled.
- **`max_duration` inside `fail_run`** — attempts remain but the budget is spent, so it
  cancels instead of retrying (its `v_task_cancel` branch, inside the retry arm at
  schema line 1188).
- **Claim expiry** — `claim_task` itself calls `fail_run` with `$ClaimTimeout` on an
  expired lease, consuming an attempt with no handler involved; enough of those exhaust
  `max_attempts` while the worker is dead.
- **`cancel_task` on an unclaimed task** (admin or user) — never claimed again.
- **A cancel or an elsewhere-failed run that DOES reach a handler** — AB001/AB002 out of
  the next durable write. Observable, unlike the rest, and still unreported: the same
  cancel landing a moment earlier is the unclaimed-task case above, so reporting only
  the mid-run one would make the log depend on a race.
- **Queue mismatch** and **unknown-task defer failure** — the SDK fails the run without
  entering the handler (`absurd_sdk:2263-2273`, `2249-2259`).

Replicating Absurd's retry-delay and duration arithmetic in Python to predict these
would duplicate pinned-SQL policy, and predicting them still would not make them Django
lifecycle events. The stored result and the queue-state models are the record of what
happened.

**Pre-persist window.** Even where we do send, SUCCESSFUL is announced before
`_complete_task_run_async` writes it (`absurd_sdk:2299`). A concurrent cancel or claim
expiry can make that write fail (AB001/AB002, swallowed), after receivers were told the
task succeeded — `get_result()` then reports FAILED.

Shutdown widens that window by however long receivers take. A hard cancel landing on a
send raises `CancelledError`, which containment deliberately does not swallow — but it
replaces the task's own exception before the handler's bare `raise`, so the SDK never
runs `_fail_task_run_async` and the attempt resurfaces later as a `$ClaimTimeout` rather
than the real error. Documented, not fixed.

## task_started per entry, not per attempt

A durable sleep or `await_event` reschedules the SAME `run_id` (`absurd.schedule_run`
updates the row in place; no insert, no attempt change), so the handler re-enters from
the top on every wake. One attempt, N entries. No suspension path creates a run or bumps
`attempt` — only `fail_run`'s retry arm and `retry_task` do.

Fire on every entry:

- Suppressing requires "did this run already execute", and the free signal cannot answer
  it. `ctx._checkpoint_cache` is preloaded by the SDK before the handler runs
  (`absurd_sdk:663-668`) but drops `owner_run_id` (line 662), and
  `get_task_checkpoint_states` selects by `task_id` filtered to owner-run attempt ≤
  current (schema 1574-1624). So `bool(cache)` cannot separate replay-of-this-run from
  attempt-2-of-a-task-that-completed-a-step — it would silently suppress `task_started`
  for every retry of a multi-step task.
- Absurd's model agrees: each wake is a fresh claim, possibly by a different worker.
  `worker_ids` is a list for exactly that reason.

**What this costs, stated plainly rather than waved off.** The N payloads for one
attempt are identical — same id, same `attempts`, same synthetic `worker_ids` — so there
is no per-entry key. A stateless receiver cannot tell replay from redelivery; a
running-gauge (inc on started, dec on finished) drifts by N−1 per sleeping task,
permanently; a plain counter over-counts with no way to correct. Entries can also arrive
from different processes, so process-local memory does not help either. So **counters
and gauges cannot be built on `task_started` from this backend.** A `run_id` on the
payload is what would fix it — part of the same upstream ask as above.

Replay _legibility_ is not Django's job — `step.replayed` / `sleep.resumed` belong to
the vocabulary unit.

## Retry: one task_finished, terminal only

Django documents no retry behaviour for the signals; `TaskResult` settles it. `READY` =
"just enqueued, **or is ready to be executed again**" (`base.py:33`, verbatim). A
non-final failed attempt is READY, not FAILED — sending FAILED per attempt makes
Django's own receiver log ERROR + traceback for a task that later succeeds.

The scarcity forces it. `TaskResultStatus` (`base.py:32-40`) has exactly four members —
`READY`, `RUNNING`, `FAILED`, `SUCCESSFUL`. No `CANCELLED`, no `RETRYING`, no
`SLEEPING`. So a failed non-final attempt has nowhere to go but `READY`, and Absurd's
six states collapse into those four, as `STATE_TO_STATUS` (`backends.py:242-249`)
already does: `sleeping` → `RUNNING`, `cancelled` → `FAILED`.

## Result id

Worker-side `TaskResult.id` becomes `f"{queue}:{task_id}"`, the same shape `enqueue`
returns (`backends.py:208`). Two defects fixed at once: `build_task_context` uses bare
`ctx.task_id`, which `decode_result_id` rejects (`rsplit(":", 1)` yields one part →
`TaskResultDoesNotExist`), and that value is a `uuid.UUID` at runtime, not the `str` the
field is annotated as (psycopg decodes the uuid columns — see the `DrainedRun`
docstring, `worker.py:64-66`). The f-string coerces it.

**`queue` comes from the worker, not from the task.** `ClaimedTask` has no queue field
(`absurd_sdk:152-164`), and the re-imported task carries its definition-time
`queue_name` — so `f"{task.queue_name}:{...}"` would label a task defined on `default`
but drained from `other` as `default:<uuid>`, and `get_result()` on that id would look
in the wrong queue's tables and raise `TaskResultDoesNotExist`. Exactly the defect this
section claims to fix, one `.using(queue_name=...)` away. The source is the plumbed
queue from Seam 2.

No internal consumer breaks: nothing reads `ctx.task_result.id`, and
`dj_absurd.get_result` already accepts both forms (`test.py:283-291`). But a
`takes_context` task that persisted that id sees stored ids change shape, so it needs a
release-note line, not just a spec paragraph.

## Deferred tasks

`build_deferred_handler` sends nothing: the `:run_after` wrapper sleeping until due is
not the caller's task starting. Its inner enqueue at wake goes through
`backend.enqueue`, so a deferred task yields **two** `task_enqueued` — the wrapper's id,
then the real task's. Both are genuine enqueues; neither is suppressed.

What holds, and matters because the payloads are otherwise nearly identical (both carry
the real `task` and the real `args`, only the id differs):

- The discriminator is `task_result.task.run_after` — set on the first, `None` on the
  second (`deferred.py:90-95` re-imports the target fresh).
- The first id is a permanent orphan: it never receives a `task_started` or
  `task_finished`, because the wrapper sends nothing. It does still round-trip through
  `get_result()`, which follows the wrapper's `completed_payload` to the real task
  (`backends.py:228-231`).
- A third `task_enqueued` is possible in the crash window between the inner spawn
  committing and its checkpoint committing, unless the caller supplied an
  `idempotency_key`.

## Known gaps

- **`task_enqueued` can fire for a task that never exists.** Enqueue runs on Django's
  connection inside the caller's transaction, and a rollback discards the spawn — but
  the signal has already been sent. Not moved to `on_commit`: that would desync our
  timing from every other Django backend. Documented instead.
- **pg_cron-scheduled tasks send no `task_enqueued`.** Their spawn is raw SQL run by
  pg_cron (`public.django_absurd_run_scheduled` → `absurd.spawn_task`), never touching
  `backend.enqueue`, so receivers see a `task_started` with no enqueue before it. Beat
  schedules DO go through the seam (`scheduler.py:76`).
- **`enqueued_at=None` at the worker.** The `t_` row does record `enqueue_at` (NOT NULL,
  schema line 156) and `get_result` reads it (`backends.py:310`) — it is the _claim
  payload_ that omits it, and our own no-DB-reads rule that makes it unreachable here.
  Ask:
  [expose `enqueue_at` on the task context](../UPSTREAM.md#expose-attempt-run_id-and-enqueue_at-on-the-task-context).
- **`worker_ids` synthetic.** `["absurd"] * attempt` keeps `attempts` and
  `TaskContext.attempt` truthful; the real `claimed_by` strings stay reachable via
  `get_result()`.
- **`errors` never accumulates** — Django documents a list across every execution; we
  send one, from this attempt. Matches existing `build_task_result`
  (`backends.py:343-351`). Pre-existing, out of scope.

## Tests — `tests/core`

Conventions: autouse `_enable_db`, so only add `django_db(transaction=True)` where a
test commits or crosses threads; anything that executes freezes time through
`dj_absurd`; no monkeypatching.

Connecting receivers needs a helper, not ten hand-rolled `try/finally`s — a
`contextmanager` in `tests/utils.py`, per the plain-function-over-fixture convention. No
test in this repo connects a `django.dispatch` signal today, so there is no precedent to
copy, and two edges must be encoded in it:

- **Connect inside the `try`** — an error between connect and `try` leaks a receiver
  into every later test in the same process.
- **Receivers must be named functions the caller keeps alive.** `Signal.connect`
  defaults to weak references, so an inline `lambda` can be garbage-collected mid-test
  and silently never fire — a test that passes for the wrong reason.

Collecting receivers must be thread-safe: the enqueue seam sends on whatever thread
called it, and a sync task body enqueueing at `concurrency>1` reaches it from several
pool threads at once, so appending to a bare list races.

### Signal level

- enqueue sends one `task_enqueued`, status `READY`, carrying the id `enqueue` returned
- `await task.aenqueue()` sends exactly one `task_enqueued`, same payload shape — the
  async entry point is covered by the sync seam, asserted rather than assumed
- drain sends `enqueued` → `started` → `finished`, `SUCCESSFUL`, return value readable
- worker-side payload id is `f"default:{uuid}"` and round-trips through
  `dj_absurd.get_result` — otherwise the id fix ships asserted only on the enqueue side
- sleeping task: `started` ×2, `finished` ×1, `attempts == 1`. Needs **two** drains:
  `enqueue()` → `drain()` (entry 1, suspends) → `frozen_time.shift(8 days)` → `drain()`
  (entry 2, finishes). The count is exactly 2 because drain 1's claim loop re-claims,
  finds `available_at` a week out under the frozen clock, and breaks
  (`worker.py:248-254`). Pattern: `test_durable.py:113-129`.
- `max_attempts=2`, always fails: `started` ×2, `finished` ×1 `FAILED`, one `TaskError`
  — in **one** drain, no clock movement. The default retry strategy is kind `'none'` →
  delay 0, so a failed non-final attempt is immediately claimable and the burst loop
  takes it. Precedent: `test_absurd_fixture.py:334-346` burns all five default attempts
  in one drain.
- `absurd_params(max_attempts=None)` + a failing task: **no** `task_finished` ever,
  across attempts — the NULL-means-retry-forever promise, which nothing else pins (the
  backend default is 5, `backends.py:117`, so only the explicit `None` reaches it)
- a receiver that raises on a worker send: the task still succeeds, one ERROR on
  `django_absurd` — the containment rule. One test suffices only because containment is
  a single `send_task_signal()` helper; inlined at three sites it would need three.
- a receiver that raises on the **enqueue** send: `enqueue()` still returns its
  `TaskResult`, one ERROR logged — the seam that would otherwise duplicate deferred
  enqueues
- deferred enqueue sends **two** `task_enqueued`: `run_after` set on the first and
  `None` on the second, and the wrapper's id never receives a `started` or `finished`.
  No new branch, but "the wrapper sends nothing" is exactly what a later refactor breaks
  silently.
- sender is the backend class, and a receiver registered `sender=AbsurdBackend` does not
  fire for an `ImmediateBackend` enqueue in the same test — `tests/settings.py:66`
  already carries the `"immediate"` alias, and `absurd.E004` counts only Absurd backends
  (`checks.py:392-398`)

### caplog on the `django.tasks` logger

No receiver of ours involved; these fail today because nothing is logged.

- enqueue → one DEBUG, `"Task id=%s path=%s enqueued backend=%s"`, filled with the
  prefixed id, the task's `module_path`, our alias. Needs
  `caplog.set_level(logging.DEBUG, logger="django.tasks")`: `tests/settings.py` sets no
  `LOGGING`, so Django's default puts `"django"` at INFO and the record is dropped at
  the logger before any handler.
- successful drain → DEBUG, then INFO `state=RUNNING`, then INFO `state=SUCCESSFUL`, in
  that order
- non-terminal failed attempt → INFO `state=RUNNING`, and **no `django.tasks` record at
  or above WARNING**. Scope by logger name: `worker.py:379-389` already emits
  `logger.exception` on the `django_absurd` logger for every failed attempt, so an
  unscoped assertion is false.
- terminal failed attempt → one ERROR, `state=FAILED`, `record.exc_info` populated and
  naming the task's own exception class, since the send sits inside the handler's
  `except` arm
- sleeping task → two INFO `state=RUNNING` and one `state=SUCCESSFUL`, the per-entry
  rule as a console shows it
- every assertion scoped by logger name, so nothing downstream can silently start
  styling `django.tasks`

### Absurd's control-flow arm

One test: a task that suspends on a durable sleep sends no `task_finished`. It pins that
`SuspendTask` does not reach the `except Exception` arm, which would report the
suspension as the task failing.

`CancelledTask`/`FailedTask` get no test, and need none — they send nothing, and the arm
that swallows them is the same one the sleep test already covers. Reaching them
specifically would need a task that sabotages its own run (cancelling itself through a
fresh SDK client, or `absurd.fail_run` on `ctx._task["run_id"]` by raw SQL) to assert an
absence, which is not worth a fixture.

## Docs

The shipped documentation is deliberately thin: ~8 lines under `## Deployment notes` in
`django_absurd/AGENTS.md` and ~13 in `docs/web/how-it-works.md`, both titled "Django
Task lifecycle logging". Between them they say only what an operator needs — Django's
`django.tasks` logger reports task lifecycle on this backend because the backend emits
Django's task signals; the log is NOT a complete record (no line marks a retried
attempt's failure, and an ending Absurd decides on its own produces none at all); and
queue state plus the stored result are the record.

No receiver-authoring material and no example receiver, because users are not invited to
build on these signals. The constraints below are the reason that is the right advice.
They live HERE rather than in the shipped docs, since writing them up for users would
amount to a guide for the thing we are declining to offer:

- A receiver's exception is contained and logged, so nothing a user connects can fail a
  task.
- A worker-side receiver runs on the worker's event loop, where Django's ORM refuses to
  run at all, and inside the run's claim lease.
- `task_started` arrives once per execution episode, not once per attempt — so counters
  and gauges built on it are wrong by construction.
- `task_finished` never arrives for an ending Absurd decided itself.
- The payload may be mutated after a receiver returns, as Django's own backends mutate
  theirs. Nothing may rely on the payload still holding its send-time state, nor on
  object identity across signals: `task_enqueued` carries a different object (often
  built in another process), and each handler entry of a task that sleeps builds a fresh
  one — identity holds only from `task_started` to `task_finished` within one entry.

The `TaskResult.id` shape change is release-note material. There is no CHANGELOG in this
repo, so it rides the commit body under `BREAKING:` for the release drafter to surface.
