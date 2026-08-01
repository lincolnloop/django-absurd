---
icon: lucide/list-checks
---

# Tasks

Everything you do day-to-day: define a task, enqueue it (with retries and other
options), and read the result. For what happens under the hood, see
[How it works](how-it-works.md).

## Define a task

Use Django's [`@task`](https://docs.djangoproject.com/en/6.0/topics/tasks/) decorator —
sync (`def`) or async (`async def`). It can live in any importable module.

```python
from django.tasks import task


@task
def send_report(user_id: int) -> None:
    ...
```

## Enqueue it

```python
result = send_report.enqueue(42)   # returns a TaskResult; a worker runs it
```

Enqueuing rides the surrounding database transaction — a task spawned inside `atomic()`
is dropped if the block rolls back.

### Run it later

Django's
[`run_after`](https://docs.djangoproject.com/en/6.0/ref/tasks/#django.tasks.Task.run_after)
defers a single enqueue to a moment of your choosing:

```python
send_report.using(run_after=timezone.now() + dt.timedelta(hours=1)).enqueue(42)
```

It takes a timezone-aware `datetime`. A deferred enqueue creates a second row named
`<your task's dotted path>:run_after` that waits, then enqueues yours with the options
you passed — both rows are visible in the admin, and the name makes deferred work
filterable by target. The id `enqueue` returned keeps working throughout: it reads
`READY` while the wrapper waits, then your task's own status and return value once it
runs. If the wrapper's own launch struggles, that id stays `READY` with no visible
errors until it runs out of attempts, then reports `FAILED`. For a repeating schedule
rather than a one-off, use [Scheduling](cron-jobs.md).

## Retries & spawn options

Absurd's spawn options (retries, retry backoff, idempotency, …) attach through one
factory, `absurd_params`, at two call sites.

**Per-task defaults — the `absurd_params(...)` decorator.** Apply it _below_ `@task`:

```python
from django.tasks import task
from django_absurd import absurd_params


@task
@absurd_params(max_attempts=3)   # this task retries up to 3 times
def send_report(user_id: int) -> None:
    ...
```

**Per-invocation — `absurd_params(...).bind(task)`.** Overrides the decorator default
for one call:

```python
from django_absurd import absurd_params

absurd_params(
    max_attempts=5,
    retry_strategy={
        "kind": "exponential",   # "fixed" | "exponential" | "none"
        "base_seconds": 2,
        "factor": 2,
        "max_seconds": 300,
    },
    idempotency_key=f"report:{42}",   # enqueue at most once per key
).bind(send_report).enqueue(42)
```

`bind` returns an ordinary `Task`: `isinstance(bound, Task)` holds, and `aenqueue`,
`call`, `get_result`, and `using` all work through it exactly as they do on the original
task.

Django's own Task API options — routing (`.using(queue_name=...)`), `backend`, … — stay
on `.using()`, never on `absurd_params`. Routing composes with binding in either order:

```python
absurd_params(max_attempts=5).bind(send_report.using(queue_name="reports")).enqueue(42)
# — or —
absurd_params(max_attempts=5).bind(send_report).using(queue_name="reports").enqueue(42)
```

`bind` attaches the params whatever backend the task is currently on, so binding and
`.using(backend=...)` also compose in either order. Only the Absurd backend reads them,
though — so if the task is still on some other backend when you enqueue it, the params
are silently inert and you get one `WARNING` naming the task and the backend it ran on
(deduped per task). Enqueue it on the Absurd backend and there is nothing to warn about.

Precedence for `max_attempts`: per-invocation → decorator default →
[`OPTIONS["DEFAULT_MAX_ATTEMPTS"]`](configuration.md#backend-options) (5).

The fields (types come from `absurd_sdk`); the "Where" column is enforced by
`absurd_params`'s own overload pair, not just convention — passing `headers` or
`idempotency_key` to the decorator form is a static and a runtime error:

| Field             | Where              | What it does                                                                             |
| ----------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `max_attempts`    | default + per-call | Retry ceiling for the task.                                                              |
| `retry_strategy`  | default + per-call | Backoff: `kind` (`fixed`/`exponential`/`none`), `base_seconds`, `factor`, `max_seconds`. |
| `cancellation`    | default + per-call | `max_duration`, `max_delay` (seconds).                                                   |
| `headers`         | per-call only      | Arbitrary JSON metadata carried with the task.                                           |
| `idempotency_key` | per-call only      | Dedupe within a queue — see the warning below.                                           |

!!! warning "An idempotency key is scoped to its queue, not to your task"

    A key reserves itself against **one queue**, with no task name and no arguments in
    the comparison. Whichever enqueue gets there first owns the key; every later
    enqueue is swallowed and handed the **first** task's id — even a different task,
    even with different arguments:

    ```python
    absurd_params(idempotency_key="nightly").bind(send_report).enqueue(42)
    absurd_params(idempotency_key="nightly").bind(purge_cache).enqueue()
    # -> same id, and purge_cache never runs
    ```

    Namespace the key yourself so it identifies the work: include the task and the
    thing it acts on, e.g. `f"send_report:{report_id}:{date}"`. The
    [beat scheduler](cron-jobs.md) does this for its own spawns — its keys are a
    `cron:`-prefixed hash of the schedule name, cron expression, and slot.

    Two more properties worth knowing:

    - **Different queues never collide.** The same key on `default` and on `reports`
      reserves independently, and both tasks run.
    - **A key is held for as long as its task row exists** — freed only once the task
      is terminal and [cleanup](cleanup.md) deletes it, `cleanup_ttl` (default 30
      days) after it finished. A task still pending, running, or sleeping holds its
      key indefinitely. So a key is not a "once per hour" window; it is "once until
      the row is swept."

The decorator's `max_attempts` and `cancellation` fields mirror the defaults accepted by
Absurd's own [task definition](https://earendil-works.github.io/absurd/)
(`default_max_attempts`, `default_cancellation`) — but not field-for-field: Absurd's
`register_task` takes no `retry_strategy`, so that field is ours alone, applied at spawn
time rather than at task definition.

→
[Absurd: retries & durable execution](https://earendil-works.github.io/absurd/concepts/).

## Read the result

`enqueue` returns a `TaskResult`; fetch one later by id:

```python
result = send_report.enqueue(42)

result = send_report.get_result(result.id)     # by id (sync)
result = await send_report.aget_result(result.id)  # async

result.status         # READY | RUNNING | SUCCESSFUL | FAILED
result.return_value   # available once SUCCESSFUL
result.errors         # populated when FAILED
```

A task may run **more than once** (at-least-once delivery), so keep handlers idempotent
— use `idempotency_key` (above) where it helps. See
[retries & runs](how-it-works.md#runs-retries-checkpoints).
