---
icon: lucide/git-branch
---

# Workflows

Break a task into checkpointed **steps**, **sleep** between them, and suspend until a
named **event** arrives — so a retry or resume never redoes completed work.

→ [Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/).

## Steps

=== "Sync"

    ```python
    from django.tasks import task
    from django_absurd import get_absurd_context


    @task
    def process_order(order_id: int) -> None:
        context = get_absurd_context()
        context.step("charge", lambda: charge_card(order_id))
        context.step("ship", lambda: ship(order_id))
    ```

=== "Async"

    ```python
    from django.tasks import task
    from django_absurd import aget_absurd_context


    @task
    async def process_order(order_id: int) -> None:
        context = aget_absurd_context()

        async def charge():
            return await charge_card(order_id)

        await context.step("charge", charge)
    ```

`context.step(name, fn)` runs `fn()`, persists the result as a checkpoint, and skips it
on replay. Reach the context from **inside** a running task — `get_absurd_context()` in
a sync task, `aget_absurd_context()` in an `async def` one. Neither needs
`takes_context=True`; add that only if you also want `context.task_result` / `.attempt`.

- **Step names and call order must be stable across replays.** Absurd locates
  checkpoints by them, so inserting, removing, or reordering any `step` or sleep call
  corrupts replay. To make an incompatible change, retire the task and introduce a new
  one.
- The async `step`'s `fn` must return an awaitable — pass an `async def`, not a plain
  lambda, which raises `TypeError`.
- Results are persisted with `json.dumps`: sets, custom classes, and `datetime` can't
  round-trip, and a `tuple` comes back as a `list`.

→
[Absurd: Concepts — Steps (Checkpoints)](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints)

### `run_step`

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

Wraps `step` for cases where a lambda is awkward. Sync only.

### Long steps

```python
def process():
    for row in big_result_set:
        process_row(row)
        context.heartbeat()   # extend the claim


context.step("process", process)
```

A run must make progress within `claim_timeout` seconds (default 120) or it is
re-claimed and replayed from the last checkpoint. Keep steps short, or heartbeat. Pass
`seconds` to extend by a specific amount instead of the worker's `claim_timeout`.

→
[Absurd: Concepts — Workers](https://earendil-works.github.io/absurd/concepts/#workers)
(the claim lease, and how a checkpoint write extends it) ·
[Retries](https://earendil-works.github.io/absurd/concepts/#retries)

## Sleep

=== "Sync"

    ```python
    @task
    def process_order(order_id: int) -> None:
        context = get_absurd_context()
        context.step("charge", lambda: charge_card(order_id))
        context.sleep_for("cooldown", 5)           # suspend for ~5 seconds
        context.step("ship", lambda: ship(order_id))
    ```

=== "Async"

    ```python
    @task
    async def process_order(order_id: int) -> None:
        context = aget_absurd_context()

        async def charge():
            return await charge_card(order_id)

        await context.step("charge", charge)
        await context.sleep_for("cooldown", 5)
    ```

The task suspends and the worker wakes and resumes it — no external scheduler.
`sleep_until("wake-up", wake_at)` is the same thing against a fixed moment, where
`wake_at` is a timezone-aware `datetime` or a Unix timestamp.

- Sleeps are checkpointed. The step name is required and shares the same namespace and
  counter as `step` calls, so it must be stable across replays too.
- A wake-up is **not a retry** — Absurd re-claims the original run and the attempt
  counter does not increment.
- Pass `sleep_until` a timezone-aware `datetime`; a naive one raises when compared
  against Absurd's clock.

→ [Absurd: Concepts — Sleep](https://earendil-works.github.io/absurd/concepts/#sleep)

## Events

=== "Sync"

    ```python
    @task
    def process_order(order_id: int) -> None:
        context = get_absurd_context()
        context.step("charge", lambda: charge_card(order_id))
        payload = context.await_event(f"warehouse.packed:{order_id}")
        context.step("ship", lambda: ship(order_id, payload))
    ```

=== "Async"

    ```python
    @task
    async def process_order(order_id: int) -> None:
        context = aget_absurd_context()
        payload = await context.await_event(f"warehouse.packed:{order_id}")

        async def ship_order():
            return await ship(order_id, payload)

        await context.step("ship", ship_order)
    ```

`await_event(name, step_name=None, timeout=None)` suspends the task until a named event
arrives and returns its JSON payload. `emit_event(name, payload=None)` emits one on the
task's own queue (replay-safe — a re-emit after a retry is a no-op).

- **First emit per name wins**, and the payload is immutable. A business-keyed name like
  `"warehouse.packed:order-42"` targets exactly one waiter.
- **Events are queue-scoped.** One emitted on queue X only wakes a waiter on queue X.
- An event emitted long before its `await_event` can be swept by the queue's
  `cleanup_ttl` first, and the waiter then never wakes. Keep `cleanup_ttl` generous
  relative to how long a waiter might sleep before checking.

→ [Absurd: Concepts — Events](https://earendil-works.github.io/absurd/concepts/#events)

### Emit from a view

```python
from django.http import HttpResponse

from django_absurd import emit_event


def warehouse_webhook(request, order):
    emit_event(f"warehouse.packed:{order}", {"tracking": request.POST["tracking"]},
               queue="default")
    return HttpResponse(status=204)
```

`context.emit_event` only reaches code running inside a task. The real-world signal that
wakes a waiter — a webhook, a view, an API handler — is ordinary Django code, and
`django_absurd.emit_event` is its entry point. End to end: the task suspends in
`await_event` (freeing the worker), the warehouse POSTs, the view emits, the task's next
claim finds it and resumes with the payload.

- `queue` must match the queue the waiting task runs on. It's a queue name, not a
  database alias.
- An undeclared queue raises `QueueNotDeclaredError`; a declared but unprovisioned one
  raises `QueueNotProvisionedError` naming `manage.py absurd_sync_queues`. See
  [exceptions](configuration.md#exceptions).
- `emit_event` is sync — wrap it in `sync_to_async` from an async view.

### Timeout

```python
import absurd_sdk


@task
def process_order(order_id: int) -> str:
    context = get_absurd_context()
    try:
        context.await_event(f"warehouse.packed:{order_id}", timeout=3600)
    except absurd_sdk.TimeoutError:
        return "gave up waiting for the warehouse"
    return "shipped"
```

Pass `timeout` (seconds) to stop waiting after a bound.

- **It is `absurd_sdk.TimeoutError`, not the builtin.** `except TimeoutError:` catches
  nothing here.
- An **uncaught** timeout fails the run, which then retries and re-waits the full
  `timeout` on each attempt until `max_attempts`. Catch it for a one-shot timeout.

## Reading headers

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    tenant = context.headers.get("tenant")
    context.step("charge", lambda: charge_card(order_id, tenant=tenant))
```

Headers passed at [enqueue time](tasks.md#retries-spawn-options) are available on
`context.headers`.

## API

Everything below is available on both contexts. On the async one, `await` the methods —
`headers` is a property, and `run_step` is sync-only.

`step(name, fn)` : Run `fn()`, checkpoint the result; skip it on replay.

`sleep_for(step_name, duration)` : Suspend the task for `duration` seconds.

`sleep_until(step_name, wake_at)` : Suspend until a `datetime` or Unix timestamp.

`await_event(event_name, step_name=None, timeout=None)` : Suspend until the named event
arrives; return its payload.

`emit_event(event_name, payload=None)` : Emit an event on the task's own queue
(replay-safe).

`heartbeat(seconds=None)` : Extend the claim timeout, keeping the run alive.

`headers` : Read-only mapping of the headers passed at enqueue time.

`run_step([name])` : Decorator wrapping `step`; derives the checkpoint name from `fn`.
Sync only.

The async context also exposes `.absurd_ctx` for anything the wrapper doesn't mirror.
There is no `await_task_result` — use Django's own
[`get_result()` / `aget_result()`](tasks.md#read-the-result) for a child task's result.

Checkpoints and waits are visible in Django admin (**Checkpoints**, **Waits**), and
inline under a task's detail page alongside its Runs.

## Gotchas

- **Never catch-all inside a task.** Absurd suspends and cancels runs via control-flow
  exceptions raised inside `step` / `sleep_for` / `sleep_until` / `await_event`. A bare
  `except:` or `except Exception:` around a durable call swallows them and silently
  breaks suspension.
- **Effectively-once, not exactly-once.** A step's result is persisted after `fn`
  returns. A crash in that window re-runs the step, so keep side effects idempotent —
  use [`idempotency_key`](tasks.md#idempotency-keys) on downstream enqueues, or make
  writes upserts.
- **Absurd backend only.** Under any other Django task backend the accessors raise
  `RuntimeError`, since the Absurd runtime context is never set. Called outside a
  running task, same.
