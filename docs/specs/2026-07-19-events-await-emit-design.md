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
- Re-export **`absurd_sdk.TimeoutError`** from `django_absurd` — `await_event`'s
  `timeout` raises it, and it is **NOT** Python's builtin `TimeoutError` (docs must
  warn; a user `except TimeoutError` on the builtin catches nothing).
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

## Top-level `emit_event` (app-side) — the outside-a-task signal

**Purpose (the "why"):** `ctx.emit_event` is only reachable _inside_ a running task, but
the real-world signal that wakes a waiter almost always arrives from **ordinary Django
code** — a webhook, a view, an API handler, an admin action. The top-level helper is
that entry point. Absurd itself describes emitting "from anywhere — another task, an API
handler, etc."
([Concepts → Events](https://earendil-works.github.io/absurd/concepts/#events)).

Event names typically carry a **business key** so an emit targets the one task waiting
on that exact name — Absurd's own example is `await_event("shipment.packed:order-42")`.
First-emit-per-name wins (immutable); payload is optional JSON.

```python
from django_absurd import emit_event

# a plain Django view / webhook / API handler (NOT a task):
def warehouse_webhook(request, order):
    emit_event(f"warehouse.packed:{order}", {"tracking": request.POST["tracking"]},
               queue="default")
    return HttpResponse(status=204)
```

End-to-end: the order task calls `await_event(f"warehouse.packed:{order}")` →
**suspends** (worker freed) → the warehouse system later POSTs the webhook → the view
emits the named event on the task's queue → the task's next claim finds it → **resumes**
with the payload.

**Contract (corrected — the earlier sketch was wrong):**

- **Not** `get_absurd_client(queue)` — that arg is a **DB alias**, not a queue, and the
  client's own queue defaults to `"default"`. Treating a queue as an alias fails
  (`ConnectionDoesNotExist` for a non-`"default"` queue) or, worse, on a non-default
  `DATABASE` **silently emits on the wrong database** and the waiter never wakes.
  Correct call: resolve the Absurd DB (`resolve_absurd_database()`), build a client on
  it (`build_absurd_client(...)`), and call the **client-level**
  `emit_event(event_name, payload, queue_name=queue)` (SDK `Absurd.emit_event` takes
  `queue_name`).
- **Validate `queue` against the declared queues** (`get_declared_queues`) and raise a
  clear error if unknown — fail fast rather than silently never-waking on a typo/wrong
  queue.
- **Savepoint + translate**, mirroring `AbsurdBackend.enqueue`: wrap the emit in
  `transaction.atomic(savepoint=True)`; a missing-table `psycopg.errors.UndefinedTable`
  (queue declared but not yet synced) → `ImproperlyConfigured` with the sync hint — so
  it fails cleanly without poisoning an enclosing transaction.
- **Module home / export:** live in a registry-safe module (e.g.
  `django_absurd/events.py`) and import `queues`/`build_absurd_client` **inside** the
  function — a top-level import in `django_absurd/__init__.py` would trip
  `AppRegistryNotReady` at app load (`queues.py` imports `models`). Re-export the
  function from `django_absurd`.
- **Sync-only** (rides Django's connection); from an async view, wrap in
  `sync_to_async`.

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

- **Docs** — an **Events** section on the Workflows page + AGENTS, **grounded in
  [Absurd → Concepts → Events](https://earendil-works.github.io/absurd/concepts/#events)**
  (link it up front; mirror its terms — events awaited by name, optional JSON payload,
  **first emit per name wins / immutable**, `timeout` → `TimeoutError`). Cover:
  `await_event` (suspend-until-signal, queue-scoped, business-key naming like
  `"warehouse.packed:order-42"` to target one waiter), in-task `emit_event`, and the
  top-level `emit_event` (the outside-a-task signal — webhook/view/API handler; show the
  suspend→emit→resume flow). Note `await_task_result` is intentionally not provided (use
  Django's `get_result` for child results). Reuse the effectively-once /
  don't-catch-all-`except` caveats, **plus these Events caveats**:
  - `TimeoutError` is **`from absurd_sdk import TimeoutError`** (re-exported by
    `django_absurd`), **not** the builtin — show the import, warn explicitly.
  - An **uncaught** `TimeoutError` fails the run, which then **retries and re-waits the
    full `timeout` each attempt** until `max_attempts` — catch it if you want a
    one-shot.
  - Events are subject to the queue's **`cleanup_ttl`**: an event emitted long before a
    delayed `await_event` can be cleaned up first → the waiter never wakes.
  - In-task `emit_event` is **replay-safe** (first-write-wins upsert → a re-emit after
    retry is a no-op).
  - `emit_event` is sync; from an **async view** wrap it in `sync_to_async`.
- **Example** — `examples/web`: the order-fulfillment task's
  `sleep_for("await-warehouse", …)` becomes `await_event(f"warehouse.packed:{order}")`;
  add a second view/button that calls the top-level
  `emit_event(f"warehouse.packed:{order}", queue="default")` to wake that specific order
  — a real signal→resume demo. Update the surrounding copy + `examples/README.md`.
- **Tests** — behavioral, real worker, both task kinds parametrized where they differ:
  - a task `await_event`s → suspends (state `sleeping`/RUNNING, a Waits row exists) → a
    separate call to the top-level `emit_event` → next drain resumes with the payload.
  - emit-before-await: event already present → `await_event` returns immediately (no
    suspend).
  - first-emit-per-name-wins (a second emit is ignored — payload unchanged).
  - **timeout (deterministic):** a task that `await_event(..., timeout=0)` and **catches
    `absurd_sdk.TimeoutError`**, returning a sentinel → assert the sentinel result in a
    single drain (avoids the retry-re-wait churn of an uncaught timeout).
  - admin: HTTP-test the task detail's Waits inline shows **the specific `event_name`
    row** (assert the value, not a row count — timeout-resume leaves stale `w_` rows).
  - Pinned timing recipe like Sleep (no monkeypatch; `transaction=True`; emit from a
    separate connection; drain → emit → drain).

## Constraints (carried from #84)

Accessor pattern (no `TaskContext` subclass); async worker + sync `run_on_loop` bridge;
thin delegation to `absurd_sdk`, mirror SDK naming/signatures; effectively-once framing;
`import typing as t` / `import datetime as dt`; absolute imports; verb-named functions;
the only durable-code `type: ignore` is `[call-arg]` on
`enqueue(absurd_spawn_params=…)`; full patch coverage; behavioral tests via real
entrypoints; assert complete message portions; docs mirror between AGENTS.md and
`docs/web/`, build clean (`uvx zensical build`).
