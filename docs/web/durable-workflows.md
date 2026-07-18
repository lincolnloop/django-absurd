---
icon: lucide/git-branch
---

# Durable workflows

Absurd's durable primitives let a task break its work into **checkpointed steps** and
**sleep** between them — persisting progress so retries and resumes pick up where they
left off, never redoing completed steps. This page covers the django-absurd surface:
`DurableContext` (sync) and `AsyncDurableContext` (async).

→ [Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/) (steps,
checkpoints, durable execution)

## Basics

Add `takes_context=True` to `@task` and type the first parameter (which **must** be
named `context`) as `DurableContext` or `AsyncDurableContext`. Both are exported from
the package root:

```python
from django_absurd import DurableContext, AsyncDurableContext
```

### Sync

```python
from django.tasks import task
from django_absurd import DurableContext


@task(takes_context=True)
def process_order(context: DurableContext, order_id: int) -> None:
    context.step("charge", lambda: charge_card(order_id))
    context.sleep_for("cooldown", 5)           # suspend for ~5 seconds
    context.step("ship", lambda: ship(order_id))
```

No `await` — sync tasks run in the worker's thread pool. All durable ops block until
complete.

### Async

The async `step`'s `fn` must return an awaitable — pass an `async def`, not a plain
lambda (a sync lambda returns a non-awaitable and raises `TypeError`):

```python
from django.tasks import task
from django_absurd import AsyncDurableContext


@task(takes_context=True)
async def process_order(context: AsyncDurableContext, order_id: int) -> None:
    async def charge():
        return await charge_card(order_id)

    await context.step("charge", charge)
    await context.sleep_for("cooldown", 5)

    async def ship_order():
        return await ship(order_id)

    await context.step("ship", ship_order)
```

## API

| Method / property                         | Sync | Async   | Description                                          |
| ----------------------------------------- | ---- | ------- | ---------------------------------------------------- |
| `step(name, fn)`                          | yes  | `await` | Run `fn()`, checkpoint the result; skip on replay    |
| `sleep_for(step_name, duration)`          | yes  | `await` | Suspend the task for `duration` seconds              |
| `sleep_until(step_name, wake_at)`         | yes  | `await` | Suspend until a `datetime`, Unix timestamp, or float |
| `heartbeat(seconds=None)`                 | yes  | `await` | Extend the claim timeout (keep the run alive)        |
| `headers`                                 | yes  | yes     | Read-only mapping of headers passed at enqueue time  |
| `run_step([name])` (decorator, sync only) | yes  | —       | Wraps `step`; derives the checkpoint name from `fn`  |

### `run_step` (sync decorator)

An alternative to `context.step` for cases where wrapping a lambda is awkward:

```python
@task(takes_context=True)
def process_order(context: DurableContext, order_id: int) -> None:
    @context.run_step                     # name = "charge"
    def charge():
        return charge_card(order_id)

    context.sleep_for("cooldown", 5)

    @context.run_step("ship-item")        # explicit name
    def ship_item():
        return ship(order_id)
```

### `sleep_until`

Sleep until a specific moment rather than a duration:

```python
import datetime as dt

@task(takes_context=True)
async def send_reminder(context: AsyncDurableContext, user_id: int) -> None:
    wake_at = dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.timezone.utc)
    await context.sleep_until("wait-for-new-year", wake_at)

    async def send():
        return await send_email(user_id)

    await context.step("send", send)
```

`wake_at` may be a timezone-aware `datetime`, or a Unix timestamp (`int` or `float`).
Pass a timezone-aware `datetime` — a naive `datetime` raises when compared against
Absurd's timezone-aware clock.

### Reading headers

Headers passed at enqueue time are available on `context.headers`:

```python
@task(takes_context=True)
def process_order(context: DurableContext, order_id: int) -> None:
    tenant = context.headers.get("tenant")
    context.step("charge", lambda: charge_card(order_id, tenant=tenant))
```

## Footguns

### (a) Effectively-once, not exactly-once

A step's result is persisted to the database after `fn` returns, on a separate
connection. In the window between `fn` completing and the checkpoint being written, a
crash causes the step to be re-run. **Keep side effects idempotent** — for example, use
`idempotency_key` on downstream enqueues or make database writes upserts.

!!! note "Effectively-once"

    Absurd guarantees **effectively-once** execution: steps are persisted after
    completion and skipped on replay. In the crash window between completion and
    persistence, a step may run more than once. This is distinct from *exactly-once*.

### (b) Deterministic naming and order

The names and call order of `step`/`sleep_for`/`sleep_until` must be **stable across
replays**. Absurd uses them to locate the right checkpoint on resume. `step` and the
sleep variants share one checkpoint namespace and counter. Inserting, removing, or
reordering any of these calls corrupts the replay and causes steps to be skipped or
replayed against the wrong checkpoint.

To make an incompatible change: retire the old task and introduce a new one.

### (c) JSON-serializable step returns

Step results are persisted with `json.dumps`. Arbitrary Python objects (sets, custom
classes, `datetime`) cannot round-trip. `tuple` values become `list` on replay — do not
match on type.

### (d) Never swallow `SuspendTask` or `CancelledTask`

Absurd uses these exceptions internally to suspend and cancel runs. A bare
`except Exception` (or broader) inside a step's `fn` will intercept them. Always
re-raise:

```python
from absurd_sdk import CancelledTask, SuspendTask


def my_step_fn():
    try:
        ...
    except (CancelledTask, SuspendTask):
        raise
    except Exception:
        ...
```

### (e) Long steps must beat `claim_timeout`

By default a run must make progress within `claim_timeout` seconds (default 120). A step
running longer than that is re-claimed and the run is replayed from the last checkpoint.
Either keep steps short or call `context.heartbeat()` periodically:

```python
@task(takes_context=True)
def process_batch(context: DurableContext, batch_id: int) -> None:
    def process():
        for row in big_result_set:
            process_row(row)
            context.heartbeat()   # extend the claim

    context.step("process", process)
```

Pass `seconds` to `heartbeat()` to extend by a specific number of seconds (default: the
queue's `claim_timeout`).

### (f) Absurd backend only

`context.step`, `sleep_for`, and `sleep_until` are Absurd-specific. Using them under any
other Django task backend raises at runtime.

### (g) Sleep resume re-claims the same run

When a sleeping task wakes, Absurd re-claims the original run — the attempt counter does
**not** increment. A sleep wake-up is not a retry.

## Enqueue a durable task

Durable tasks enqueue the same way as any other task:

```python
process_order.enqueue(order_id=42)
```

The task suspends at each `sleep_for`/`sleep_until` call, and the worker wakes and
resumes it — no external scheduler needed.

## Admin introspection

Checkpoints are visible in Django admin under **Checkpoints** (one row per completed
step). The `available_at` column — a sleeping run's wake time — appears on the run
detail page (Claim fieldset) and in the task detail's Runs inline.

→ [How it works — runs, retries & checkpoints](how-it-works.md#runs-retries-checkpoints)
