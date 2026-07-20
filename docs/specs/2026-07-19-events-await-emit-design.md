# Events: await_event + emit_event — Design

**Goal:** expose Absurd's **Events** pillar to django-absurd tasks — a task suspends
until a named event arrives (`await_event`), and events are emitted both from inside a
task and from outside (a view). Completes the meaningful remainder of #21 (durable
sleep/wait/await_event); **closes #21**.

Builds directly on the shipped Steps + Sleep accessor pattern (#84):
`get_absurd_context()` (sync bridge `AbsurdTaskContext`) / `aget_absurd_context()`
(async — the SDK's `AsyncTaskContext` passthrough). Grounded in the SDK source
(`absurd_sdk/__init__.py`) and
[Absurd — Concepts → Events](https://earendil-works.github.io/absurd/concepts/).

## Scope

IN:

- `await_event(event_name, step_name=None, timeout=None)` — in-task,
  suspend-until-signal, first-emit-per-name-wins, optional timeout; returns the event
  payload.
- `emit_event(event_name, payload=None)` — in-task (on the task's own queue).
- **Top-level `django_absurd.emit_event(event_name, payload=None, *, queue="default")`**
  — emit from _outside_ a task (e.g. a view) to wake a waiter.
- **Waits admin inline** under the task (mirrors the shipped Checkpoints inline).
- Docs + tests + example (order-fulfillment's `sleep` stand-in → real `await_event`).

OUT (deliberately not built):

- **`await_task_result`** — the SDK's version polls + heartbeats inside a step (it does
  NOT suspend — it **holds the worker slot**), is cross-queue-only (same-queue
  deadlocks), and Django's `get_result()`/`aget_result()` already covers fetching a
  child's result. Its only marginal add is checkpointed fan-out/join in one task body —
  an advanced pattern with sharp edges. Use `get_result` for child results; revisit only
  on real demand.

## Events are queue-scoped (load-bearing)

Verified in the SDK + schema: `ctx.emit_event` writes
`absurd.emit_event(self._queue_name, …)` ("this task's queue"); `ctx.await_event` reads
`absurd.await_event(self._queue_name, …)`; events live in per-queue tables
(`e_<queue>`); first-emit-per-name-wins is **per queue**. So an event emitted on queue X
only satisfies `await_event` for tasks waiting on queue X. The top-level `emit_event`
helper therefore takes `queue` — you emit to the queue the awaiting task runs on.

## Async is free; sync is a thin bridge

`aget_absurd_context()` already returns the SDK `AsyncTaskContext`, which already has
async `await_event`/`emit_event` — **async tasks can call them today**
(`await context.await_event(...)`). No new production code for the async path — docs +
tests only.

Sync tasks need the bridge: add to `AbsurdTaskContext` (mirroring the shipped
`sleep_for`/`heartbeat` bridges via `run_on_loop`), SDK signatures verbatim:

```python
def await_event(self, event_name, step_name=None, timeout=None) -> JsonValue:
    # bridge self.absurd_ctx.await_event(...); returns payload;
    # raises SuspendTask (→ existing control-flow arm) when it suspends,
    # or TimeoutError when timeout elapses.

def emit_event(self, event_name, payload=None) -> None:
    # bridge self.absurd_ctx.emit_event(...)
```

`await_event` suspending → the worker's existing `except (SuspendTask, …)` control-flow
arm already re-raises it (no worker change needed). `emit_event` is fire-and-forget.

## Top-level `emit_event` (app-side)

A thin public helper, mirroring Absurd's own `app.emit_event` vs `ctx.emit_event` split,
so a view can wake a waiter without reaching into internals:

```python
from django_absurd import emit_event

emit_event("warehouse.packed", {"tracking": "XYZ"}, queue="default")
```

~8 lines: resolve the Absurd DB, open a client on `queue` (the existing
`get_absurd_client(queue)` path), call its `emit_event(event_name, payload)`, done. Runs
on the Absurd connection; no task context required.

## Waits admin

`await_event` populates the `waits` view/entity (columns `task_id`, `run_id`,
`step_name`, `event_name`, `timeout_at`, `created_at`). Surface it under the task — a
read-only **Waits inline**, mirroring the shipped Checkpoints inline exactly:

- `admin_views.build_model_field`: add a
  `spec.name == "waits" and col_name == "task_id"` branch returning a constraint-free
  `task` FK (`to_field="task_id"`, `db_column="task_id"`, `db_constraint=False`,
  `DO_NOTHING`, `null=True`, `related_name="waits"`); change the `waits`
  `EntitySpec.search_fields` `"task_id"` → `"task__task_id"`.
- `admin.py`: `WAIT_INLINE_FIELDS` (`event_name`, `step_name`, `timeout_at`,
  `created_at`), a `ReadOnlyWaitInline` (read-only perms, `fk_name="task"`,
  `show_change_link`, `ordering=("created_at",)`), a `build_wait_inline`, wired into the
  tasks admin `inlines` alongside Runs + Checkpoints.

## Docs, tests, example

- **Docs** — an **Events** section on the Workflows page + AGENTS: `await_event`
  (suspend-until-signal, first-emit-wins, `timeout`, queue-scoped), in-task
  `emit_event`, and the top-level `emit_event`; link Absurd's Concepts → Events. Note
  `await_task_result` is intentionally not provided (use Django's `get_result` for child
  results). Reuse the effectively-once / don't-catch-all-`except` caveats.
- **Example** — `examples/web`: the order-fulfillment task's
  `sleep_for("await-warehouse", …)` becomes `await_event("warehouse.packed")`; add a
  second view/button that calls the top-level `emit_event("warehouse.packed", queue=…)`
  to wake it — a real signal→resume demo. Update the surrounding copy +
  `examples/README.md`.
- **Tests** — behavioral, real worker, both task kinds parametrized where they differ:
  - a task `await_event`s → suspends (state `sleeping`/RUNNING, a Waits row exists) → a
    separate call to the top-level `emit_event` → next drain resumes with the payload.
  - first-emit-per-name-wins (a second emit is ignored).
  - `timeout` elapses → `TimeoutError` path (task fails or handles it — assert
    observable).
  - admin: HTTP-test the task detail shows the Waits inline (event_name) while
    suspended.
  - Pinned timing recipe like Sleep (no monkeypatch; `transaction=True`).

## Constraints (carried from #84)

Accessor pattern (no `TaskContext` subclass); async worker + sync `run_on_loop` bridge;
thin delegation to `absurd_sdk`, mirror SDK naming/signatures; effectively-once framing;
`import typing as t` / `import datetime as dt`; absolute imports; verb-named functions;
the only durable-code `type: ignore` is `[call-arg]` on
`enqueue(absurd_spawn_params=…)`; full patch coverage; behavioral tests via real
entrypoints; assert complete message portions; docs mirror between AGENTS.md and
`docs/web/`, build clean (`uvx zensical build`).
