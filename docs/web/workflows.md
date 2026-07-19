---
icon: lucide/git-branch
---

# Workflows

Absurd calls these primitives **Steps (Checkpoints)** and **Sleep** — see
[Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/). They let a task
break its work into checkpointed steps and sleep between them, persisting progress so
retries and resumes pick up where they left off, never redoing completed steps. This
page covers the django-absurd surface: the `get_absurd_context()` /
`aget_absurd_context()` accessors.

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

## API

| Method / property                         | Sync | Async   | Description                                          |
| ----------------------------------------- | ---- | ------- | ---------------------------------------------------- |
| `step(name, fn)`                          | yes  | `await` | Run `fn()`, checkpoint the result; skip on replay    |
| `sleep_for(step_name, duration)`          | yes  | `await` | Suspend the task for `duration` seconds              |
| `sleep_until(step_name, wake_at)`         | yes  | `await` | Suspend until a `datetime`, Unix timestamp, or float |
| `heartbeat(seconds=None)`                 | yes  | `await` | Extend the claim timeout (keep the run alive)        |
| `headers`                                 | yes  | yes     | Read-only mapping of headers passed at enqueue time  |
| `run_step([name])` (decorator, sync only) | yes  | —       | Wraps `step`; derives the checkpoint name from `fn`  |

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

## Enqueue a durable task

Durable tasks enqueue the same way as any other task:

```python
process_order.enqueue(order_id=42)
```

## Admin introspection

Checkpoints are visible in Django admin under **Checkpoints** (one row per completed
step). The `available_at` column — a sleeping run's wake time — appears on the run
detail page (Claim fieldset) and in the task detail's Runs inline.

→ [How it works — runs, retries & checkpoints](how-it-works.md#runs-retries-checkpoints)
