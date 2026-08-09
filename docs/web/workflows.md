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

`step(name, fn)` runs `fn()`, checkpoints the result, and skips it on replay. Get the
context **inside** a running task — `get_absurd_context()` when sync,
`aget_absurd_context()` when `async def`. Neither needs Django's own
[`takes_context`](https://docs.djangoproject.com/en/6.0/ref/tasks/#task-context).

- **Step names and call order must be stable across replays** — Absurd finds checkpoints
  by them. Inserting, removing, or reordering a `step` or sleep corrupts replay; retire
  the task and add a new one instead.
- Async `fn` must return an awaitable — an `async def`, not a lambda.
- Results go through `json.dumps`: no sets, custom classes, or `datetime`, and `tuple`
  returns as `list`.

→
[Absurd: Concepts — Steps (Checkpoints)](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints)

### `run_step`

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()

    @context.run_step  # name = "charge"
    def charge():
        return charge_card(order_id)

    @context.run_step("ship-item")  # explicit name
    def ship_item():
        return ship(order_id)
```

Wraps `step` for cases where a lambda is awkward. Sync only.

### Long steps

```python
def process():
    for row in big_result_set:
        process_row(row)
        context.heartbeat()  # extend the claim


context.step("process", process)
```

A run must make progress within `claim_timeout` seconds (default 120) or it is
re-claimed and replayed from the last checkpoint. Keep steps short, or heartbeat.
`seconds` extends by a specific amount.

→
[Absurd: Concepts — Workers](https://earendil-works.github.io/absurd/concepts/#workers)

## Sleep

=== "Sync"

    ```python
    @task
    def process_order(order_id: int) -> None:
        context = get_absurd_context()
        context.step("charge", lambda: charge_card(order_id))
        context.sleep_for("cooldown", 5)  # suspend for ~5 seconds
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

The worker wakes and resumes the task — no external scheduler.
`sleep_until("wake-up", wake_at)` does the same against a fixed moment.

- Sleeps are checkpointed, and their step names share the namespace and counter with
  `step`, so they must be stable across replays too.
- A wake-up is **not a retry** — the original run is re-claimed and `attempt` doesn't
  increment.
- `sleep_until` takes a timezone-aware `datetime` or a Unix timestamp; a naive
  `datetime` raises.

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

- **First emit per name wins**, and the payload is immutable — so a business-keyed name
  like `"warehouse.packed:order-42"` targets exactly one waiter.
- **Events are queue-scoped.** One emitted on queue X only wakes a waiter on queue X.
- An event emitted long before its `await_event` can be swept by `cleanup_ttl` first,
  and the waiter never wakes. Keep the TTL generous.

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

`context.emit_event` only reaches code inside a task. The signal that actually wakes a
waiter — a webhook, a view, an API handler — is ordinary Django code, so it uses the
top-level `django_absurd.emit_event` instead.

- `queue` must match the queue the waiting task runs on. A queue name, not a database
  alias.
- Undeclared raises `QueueNotDeclaredError`; declared but unprovisioned raises
  `QueueNotProvisionedError`. See [exceptions](configuration.md#exceptions).
- It's sync — wrap in `sync_to_async` from an async view.

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

- **It is `absurd_sdk.TimeoutError`, not the builtin.** `except TimeoutError:` catches
  nothing here.
- **Uncaught, it fails the run**, which retries and re-waits the full `timeout` each
  attempt until `max_attempts`. Catch it for a one-shot timeout.

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

<!-- prettier-ignore-start -->

`step(name, fn)`
: Run `fn()`, checkpoint the result; skip it on replay.

`sleep_for(step_name, duration)`
: Suspend the task for `duration` seconds.

`sleep_until(step_name, wake_at)`
: Suspend until a `datetime` or Unix timestamp.

`await_event(event_name, step_name=None, timeout=None)`
: Suspend until the named event arrives; return its payload.

`emit_event(event_name, payload=None)`
: Emit an event on the task's own queue (replay-safe).

`heartbeat(seconds=None)`
: Extend the claim timeout, keeping the run alive.

`headers`
: Read-only mapping of the headers passed at enqueue time.

`run_step([name])`
: Decorator wrapping `step`; derives the checkpoint name from `fn`. Sync only.

<!-- prettier-ignore-end -->

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
