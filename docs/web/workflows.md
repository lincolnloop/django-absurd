---
icon: lucide/git-branch
---

# Workflows

Absurd calls these primitives **Steps (Checkpoints)**, **Sleep**, and **Events** — see
[Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/). They let a task
break its work into checkpointed steps, sleep between them, and suspend until a named
signal arrives — persisting progress so retries and resumes pick up where they left off,
never redoing completed steps. This page covers the django-absurd surface: the
`get_absurd_context()` / `aget_absurd_context()` accessors.

## Basics

Call the matching accessor **inside** a running task to reach the durable primitives.
Both are orthogonal to Django's `TaskContext` — you do **not** need `takes_context=True`
(add that only if you also want `context.task_result` / `.attempt`).

```python
from django_absurd import aget_absurd_context, get_absurd_context
```

Pick the accessor by task kind — each returns one concrete, fully-typed context, so
there is no cast and no union to narrow:

- **Sync task → `get_absurd_context()`** returns `django_absurd.AbsurdTaskContext`, a
  thin bridge mirroring the SDK's sync signatures (no `await`); it also carries
  `run_step` (sync only).
- **Async task → `aget_absurd_context()`** returns the SDK's own
  `absurd_sdk.AsyncTaskContext` (a py.typed object) — pure passthrough, you `await` its
  methods.

Called outside a running Absurd task, either accessor raises `RuntimeError`.

## Steps (checkpoints)

`context.step(name, fn)` runs `fn()`, persists the result as a checkpoint, and skips it
on replay — the core of durable execution. Step names and call order must be
**deterministic and stable** across replays: Absurd uses them to locate the right
checkpoint on resume. Inserting, removing, or reordering any `step` or sleep call
corrupts replay. To make an incompatible change, retire the old task and introduce a new
one.

→
[Absurd: Concepts — Steps (Checkpoints)](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints)

### Sync

```python
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    context.step("ship", lambda: ship(order_id))
```

No `await` — sync tasks run in the worker's thread pool. All durable ops block until
complete.

### Async

The async `step`'s `fn` must return an awaitable — pass an `async def`, not a plain
lambda (a sync lambda returns a non-awaitable and raises `TypeError`):

```python
from django.tasks import task
from django_absurd import aget_absurd_context


@task
async def process_order(order_id: int) -> None:
    context = aget_absurd_context()

    async def charge():
        return await charge_card(order_id)

    await context.step("charge", charge)

    async def ship_order():
        return await ship(order_id)

    await context.step("ship", ship_order)
```

### `run_step` (sync decorator)

An alternative to `context.step` for cases where wrapping a lambda is awkward:

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()

    @context.run_step                     # name = "charge"
    def charge():
        return charge_card(order_id)

    @context.run_step("ship-item")        # explicit name
    def ship_item():
        return ship(order_id)
```

### Long steps and `heartbeat`

By default a run must make progress within `claim_timeout` seconds (default 120). A step
running longer than that is re-claimed and the run is replayed from the last checkpoint.
Either keep steps short or call `context.heartbeat()` periodically:

```python
@task
def process_batch(batch_id: int) -> None:
    context = get_absurd_context()

    def process():
        for row in big_result_set:
            process_row(row)
            context.heartbeat()   # extend the claim

    context.step("process", process)
```

Pass `seconds` to `heartbeat()` to extend by a specific number of seconds (default: the
worker's `claim_timeout`).

### Step return values

Step results are persisted with `json.dumps`. Arbitrary Python objects (sets, custom
classes, `datetime`) cannot round-trip. `tuple` values become `list` on replay — do not
match on type.

## Sleep

`context.sleep_for(step_name, duration)` suspends the task for `duration` seconds.
`context.sleep_until(step_name, wake_at)` suspends until a specific moment. Both are
checkpointed — the step name is required and shares the same namespace and counter as
`step` calls; it must be stable across replays.

The task suspends at each sleep call and the worker wakes and resumes it — no external
scheduler needed. When a sleeping task wakes, Absurd re-claims the original run — the
attempt counter does **not** increment. A sleep wake-up is not a retry.

→ [Absurd: Concepts — Sleep](https://earendil-works.github.io/absurd/concepts/#sleep)

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    context.sleep_for("cooldown", 5)           # suspend for ~5 seconds
    context.step("ship", lambda: ship(order_id))
```

### `sleep_until`

Sleep until a specific moment rather than a duration:

```python
import datetime as dt

from django_absurd import aget_absurd_context


@task
async def send_reminder(user_id: int) -> None:
    context = aget_absurd_context()
    wake_at = dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.timezone.utc)
    await context.sleep_until("wait-for-new-year", wake_at)

    async def send():
        return await send_email(user_id)

    await context.step("send", send)
```

`wake_at` may be a timezone-aware `datetime`, or a Unix timestamp (`int` or `float`).
Pass a timezone-aware `datetime` — a naive `datetime` raises when compared against
Absurd's timezone-aware clock.

## Events

`context.await_event(event_name, step_name=None, timeout=None)` suspends the task until
a named event arrives, then returns its JSON payload.
`context.emit_event(event_name, payload=None)` emits an event on the task's own queue
(in-task, replay-safe — a re-emit after a retry is a no-op). Events are awaited by name,
carry an optional JSON payload, and **first emit per name wins** (immutable) — a
business-keyed name like `"warehouse.packed:order-42"` targets exactly one waiter.

→ [Absurd: Concepts — Events](https://earendil-works.github.io/absurd/concepts/#events)

Events are **queue-scoped**: `await_event`/`emit_event` operate on the task's own queue.
An event emitted on queue X only wakes a waiter on queue X.

### The outside-a-task signal: top-level `emit_event`

`ctx.emit_event` only reaches code running _inside_ a task. The real-world signal that
wakes a waiter — a webhook, a view, an API handler — is ordinary Django code, not a
task. `django_absurd.emit_event(event_name, payload=None, *, queue="default")` is that
entry point:

```python
from django_absurd import emit_event


def warehouse_webhook(request, order):
    emit_event(f"warehouse.packed:{order}", {"tracking": request.POST["tracking"]},
               queue="default")
    return HttpResponse(status=204)
```

End-to-end: a task calls `await_event(f"warehouse.packed:{order}")` → suspends (worker
freed) → the warehouse system POSTs the webhook → the view emits the event on the task's
queue → the task's next claim finds it → resumes with the payload.

`queue` must match the queue the waiting task actually runs on — it targets the
client-level `emit_event`'s `queue_name`, not a database alias. An unknown queue raises
`ImproperlyConfigured` immediately (fail fast on a typo). `emit_event` is sync; from an
async view, wrap it in `sync_to_async`.

### Sync

```python
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    payload = context.await_event(f"warehouse.packed:{order_id}")
    context.step("ship", lambda: ship(order_id, payload))
```

### Async

```python
from django.tasks import task
from django_absurd import aget_absurd_context


@task
async def process_order(order_id: int) -> None:
    context = aget_absurd_context()
    payload = await context.await_event(f"warehouse.packed:{order_id}")

    async def ship_order():
        return await ship(order_id, payload)

    await context.step("ship", ship_order)
```

### Timeout

Pass `timeout` (seconds) to stop waiting after a bound. On timeout, `await_event` raises
`absurd_sdk.TimeoutError` — **not** the builtin `TimeoutError`:

```python
import absurd_sdk
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> str:
    context = get_absurd_context()
    try:
        context.await_event(f"warehouse.packed:{order_id}", timeout=3600)
    except absurd_sdk.TimeoutError:
        return "gave up waiting for the warehouse"
    return "shipped"
```

!!! warning "Not the builtin `TimeoutError`"

    `except TimeoutError:` (the builtin) does **not** catch this — you must
    `import absurd_sdk` and catch `absurd_sdk.TimeoutError` explicitly.

An **uncaught** `TimeoutError` fails the run, which then retries and re-waits the full
`timeout` on each attempt until `max_attempts` — catch it if you want a one-shot
timeout.

### `await_task_result` is not provided

Absurd's SDK version of this polls + heartbeats inside a step rather than suspending
(holding the worker slot), and is cross-queue-only. For a child task's result, use
Django's `get_result()` / `aget_result()` instead.

## API

| Method / property                                       | Sync | Async   | Description                                               |
| ------------------------------------------------------- | ---- | ------- | --------------------------------------------------------- |
| `step(name, fn)`                                        | yes  | `await` | Run `fn()`, checkpoint the result; skip on replay         |
| `sleep_for(step_name, duration)`                        | yes  | `await` | Suspend the task for `duration` seconds                   |
| `sleep_until(step_name, wake_at)`                       | yes  | `await` | Suspend until a `datetime`, Unix timestamp, or float      |
| `await_event(event_name, step_name=None, timeout=None)` | yes  | `await` | Suspend until the named event arrives; return its payload |     |
| `emit_event(event_name, payload=None)`                  | yes  | `await` | Emit an event on the task's own queue (replay-safe)       |
| `heartbeat(seconds=None)`                               | yes  | `await` | Extend the claim timeout (keep the run alive)             |
| `headers`                                               | yes  | yes     | Read-only mapping of headers passed at enqueue time       |
| `run_step([name])` (decorator, sync only)               | yes  | —       | Wraps `step`; derives the checkpoint name from `fn`       |

### Reading headers

Headers passed at enqueue time are available on `context.headers`:

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    tenant = context.headers.get("tenant")
    context.step("charge", lambda: charge_card(order_id, tenant=tenant))
```

## Caveats

### Effectively-once, not exactly-once

A step's result is persisted to the database after `fn` returns, on a separate
connection. In the window between `fn` completing and the checkpoint being written, a
crash causes the step to be re-run. **Keep side effects idempotent** — for example, use
`idempotency_key` on downstream enqueues or make database writes upserts.

!!! note "Effectively-once"

    Absurd guarantees **effectively-once** execution: steps are persisted after
    completion and skipped on replay. In the crash window between completion and
    persistence, a step may run more than once. This is distinct from *exactly-once*.

### Don't catch-all `except` in a task

Absurd suspends and cancels runs via control-flow exceptions raised inside
`step`/`sleep_for`/`sleep_until`. A bare `except:` or `except Exception:` around a
durable call swallows them and silently breaks suspension — let them propagate.

### Absurd backend only

`get_absurd_context()` / `aget_absurd_context()` (and `step` / `sleep_for` /
`sleep_until` on the returned context) are Absurd-specific. Called under any other
Django task backend — where the Absurd runtime context is never set — they raise
`RuntimeError`.

### Events are subject to cleanup_ttl

An event emitted long before a delayed `await_event` can be cleaned up by the queue's
`cleanup_ttl` before the waiter ever checks — the waiter then never wakes. Keep
`cleanup_ttl` generous relative to how long a waiter might sleep before checking.

### `TimeoutError` is `absurd_sdk.TimeoutError`, not the builtin

`except TimeoutError:` silently catches nothing — `import absurd_sdk` and catch
`absurd_sdk.TimeoutError`.

## Enqueue a durable task

Durable tasks enqueue the same way as any other task:

```python
process_order.enqueue(order_id=42)
```

## Admin introspection

Checkpoints are visible in Django admin under **Checkpoints** (one row per completed
step). The `available_at` column — a sleeping run's wake time — appears on the run
detail page (Claim fieldset) and in the task detail's Runs inline.

Waits are visible in Django admin under **Waits** (one row per task suspended in
`await_event`), and inline under the task detail page alongside Runs and Checkpoints.

→ [How it works — runs, retries & checkpoints](how-it-works.md#runs-retries-checkpoints)
