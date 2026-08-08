---
icon: lucide/list-checks
---

# Tasks

Enqueue background work and read the result. For what happens under the hood, see
[How it works](how-it-works.md).

## Enqueue

```python
from django.tasks import task


@task
def send_report(user_id: int) -> None: ...


result = send_report.enqueue(42)  # returns a TaskResult; a worker runs it
```

A [`@task`](https://docs.djangoproject.com/en/6.0/topics/tasks/) lives in any importable
module. `async def` works the same — `await send_report.aenqueue(42)`.

- Enqueuing rides the surrounding transaction, so an `atomic()` rollback drops the task.
- Delivery is **at-least-once** — keep handlers idempotent. See
  [runs & retries](how-it-works.md#runs-retries-checkpoints).

## Read the result

```python
result = send_report.enqueue(42)

result = send_report.get_result(result.id)  # by id, sync
result = await send_report.aget_result(result.id)  # async

result.status  # READY | RUNNING | SUCCESSFUL | FAILED
result.return_value  # available once SUCCESSFUL
result.errors  # populated when FAILED
```

Ids are `"<queue>:<uuid>"` — the same value `context.task_result.id` reports inside a
`takes_context` task, so either can go straight back to `get_result`.

→ [Django: task results](https://docs.djangoproject.com/en/6.0/ref/tasks/#task-results).

## Run it later

```python
send_report.using(run_after=timezone.now() + dt.timedelta(hours=1)).enqueue(42)
```

Django's
[`run_after`](https://docs.djangoproject.com/en/6.0/ref/tasks/#django.tasks.Task.run_after)
defers one enqueue, taking a timezone-aware `datetime`. For a repeating schedule, use
[Cron Jobs](cron-jobs.md).

- A wrapper row named `<task path>:run_after` waits, then enqueues yours. Both appear in
  the admin.
- The id you got back keeps working: `READY` while the wrapper waits, then your task's
  own status and result. A wrapper that can't launch stays `READY` with no visible error
  until it exhausts its attempts.

## Retries & spawn options

Absurd's spawn options — retries, backoff, cancellation, headers, idempotency — attach
through one factory, `absurd_params`, at two call sites.

### Per-task defaults

```python
from django.tasks import task
from django_absurd import absurd_params


@task
@absurd_params(max_attempts=3)  # apply BELOW @task
def send_report(user_id: int) -> None: ...
```

### Per-invocation

```python
from django_absurd import absurd_params

absurd_params(
    max_attempts=5,
    retry_strategy={
        "kind": "exponential",  # "fixed" | "exponential" | "none"
        "base_seconds": 2,
        "factor": 2,
        "max_seconds": 300,
    },
).bind(send_report).enqueue(42)
```

`bind` overrides the decorator default for one call. Precedence for `max_attempts`:
per-invocation → decorator →
[`OPTIONS["DEFAULT_MAX_ATTEMPTS"]`](configuration.md#backend-options) (5).

| Field             | Where                | Default                                                          | What it does                                                                             |
| ----------------- | -------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `max_attempts`    | decorator + per-call | [`DEFAULT_MAX_ATTEMPTS`](configuration.md#backend-options) (`5`) | Retry ceiling; `None` means retry forever.                                               |
| `retry_strategy`  | decorator + per-call | `kind: "none"` — retry immediately, no backoff                   | Backoff: `kind` (`fixed`/`exponential`/`none`), `base_seconds`, `factor`, `max_seconds`. |
| `cancellation`    | decorator + per-call | unset — no time limit                                            | `max_duration`, `max_delay` (seconds).                                                   |
| `headers`         | per-call only        | unset                                                            | Arbitrary JSON metadata carried with the task.                                           |
| `idempotency_key` | per-call only        | unset — no deduping                                              | Dedupe within a queue — see [below](#idempotency-keys).                                  |

- **Backoff defaults, once you pick a `kind`:** `fixed` waits `base_seconds` (`60`);
  `exponential` waits `base_seconds` (`30`) × `factor` (`2`) ^ (attempt − 1), uncapped
  unless you set `max_seconds`.
- `headers` and `idempotency_key` on the decorator form are an error, statically and at
  runtime.
- `bind` returns an ordinary `Task`, so `aenqueue`, `call`, `get_result`, and `using`
  all still work.
- Django's own options stay on
  [`.using()`](https://docs.djangoproject.com/en/6.0/ref/tasks/#django.tasks.Task.using),
  never on `absurd_params`. They compose in either order.
- `max_attempts=None` means **retry forever** — and only an explicit `None` does, since
  omitting it fills in the default. Such a task is never terminal, so Django's task
  logger never records a final line.
- On a non-Absurd backend the params are inert, with one `WARNING` per task.

→
[Absurd: retries & durable execution](https://earendil-works.github.io/absurd/concepts/).

## Idempotency keys

```python
absurd_params(
    idempotency_key=f"send_report:{user_id}:{date}",
).bind(send_report).enqueue(42)
```

Whichever enqueue reaches a key first owns it; later ones are swallowed and handed the
**first** task's id. The comparison is the key alone — no task name, no arguments — so
namespace it yourself or unrelated work collides:

```python
absurd_params(idempotency_key="nightly").bind(send_report).enqueue(42)
absurd_params(idempotency_key="nightly").bind(purge_cache).enqueue()
# -> same id, and purge_cache never runs
```

- **Scoped to one queue.** The same key on `default` and on `reports` reserves
  independently, and both run.
- **Held as long as the task row exists** — freed only once the task is terminal and
  [cleanup](cleanup.md) deletes it, `cleanup_ttl` (default 30 days) later. Not "once per
  hour"; "once until the row is swept".
- The [beat scheduler](cron-jobs.md) namespaces its own: a `cron:`-prefixed hash of the
  schedule name, expression, and slot.

→
[Absurd: idempotency keys](https://earendil-works.github.io/absurd/concepts/#idempotency-keys).
