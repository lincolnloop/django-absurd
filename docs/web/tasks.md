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

## Retries & spawn options

Absurd's spawn options (retries, retry backoff, idempotency, …) attach two ways.

**Per-task defaults — the `@absurd_default_params` decorator.** Apply it _below_
`@task`:

```python
from django.tasks import task
from django_absurd.params import absurd_default_params


@task
@absurd_default_params(max_attempts=3)   # this task retries up to 3 times
def send_report(user_id: int) -> None:
    ...
```

**Per-enqueue — `absurd_spawn_params`.** Overrides the decorator default for one call:

```python
from django_absurd.params import AbsurdSpawnParams

send_report.enqueue(
    42,
    absurd_spawn_params=AbsurdSpawnParams(
        max_attempts=5,
        retry_strategy={
            "kind": "exponential",   # "fixed" | "exponential" | "none"
            "base_seconds": 2,
            "factor": 2,
            "max_seconds": 300,
        },
        idempotency_key=f"report:{42}",   # enqueue at most once per key
    ),
)
```

Precedence for `max_attempts`: per-call → decorator default →
[`OPTIONS["DEFAULT_MAX_ATTEMPTS"]`](configuration.md#backend-options) (5).

The fields (types come from `absurd_sdk`):

| Field             | Where              | What it does                                                                             |
| ----------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `max_attempts`    | default + per-call | Retry ceiling for the task.                                                              |
| `retry_strategy`  | default + per-call | Backoff: `kind` (`fixed`/`exponential`/`none`), `base_seconds`, `factor`, `max_seconds`. |
| `cancellation`    | default + per-call | `max_duration`, `max_delay` (seconds).                                                   |
| `headers`         | per-call only      | Arbitrary JSON metadata carried with the task.                                           |
| `idempotency_key` | per-call only      | Dedupe — a repeat enqueue with the same key is a no-op.                                  |

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
