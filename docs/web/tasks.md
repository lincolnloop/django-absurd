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

A [`@task`](https://docs.djangoproject.com/en/6.0/topics/tasks/) can live in any
importable module, and `async def` works the same way — enqueue it with
`await send_report.aenqueue(42)`.

- Enqueuing rides the surrounding transaction. A task enqueued inside `atomic()` is
  dropped if the block rolls back.
- A task may run **more than once** (at-least-once delivery) — keep handlers idempotent.
  See [runs & retries](how-it-works.md#runs-retries-checkpoints).

## Read the result

```python
result = send_report.enqueue(42)

result = send_report.get_result(result.id)  # by id, sync
result = await send_report.aget_result(result.id)  # async

result.status  # READY | RUNNING | SUCCESSFUL | FAILED
result.return_value  # available once SUCCESSFUL
result.errors  # populated when FAILED
```

Ids are the `"<queue>:<uuid>"` form. `context.task_result.id` reports the same value
inside a `takes_context` task, so either can be handed straight back to `get_result`.

## Run it later

```python
send_report.using(run_after=timezone.now() + dt.timedelta(hours=1)).enqueue(42)
```

Django's
[`run_after`](https://docs.djangoproject.com/en/6.0/ref/tasks/#django.tasks.Task.run_after)
defers a single enqueue. It takes a timezone-aware `datetime`. For a repeating schedule
rather than a one-off, use [Cron Jobs](cron-jobs.md).

- A second row named `<your task's dotted path>:run_after` waits, then enqueues yours
  with the options you passed. Both rows show up in the admin.
- The id `enqueue` returned keeps working throughout: `READY` while the wrapper waits,
  then your task's own status and return value. If the wrapper itself can't launch, that
  id stays `READY` with no visible error until it runs out of attempts, then `FAILED`.

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
- Passing `headers` or `idempotency_key` to the decorator form is an error, statically
  and at runtime.
- `bind` returns an ordinary `Task` — `aenqueue`, `call`, `get_result`, and `using` all
  work through it.
- Django's own options stay on `.using()`, never on `absurd_params`. The two compose in
  either order: `absurd_params(...).bind(send_report.using(queue_name="reports"))` and
  `absurd_params(...).bind(send_report).using(queue_name="reports")` are equivalent.
- `max_attempts=None` means **retry forever**, and only an explicit `None` does — omit
  it and the backend fills in its default on every enqueue. Such a task is never
  terminal, so Django's task logger never records a final line for it.
- On a non-Absurd backend the params are silently inert, and you get one `WARNING`
  naming the task and the backend (deduped per task).

→
[Absurd: retries & durable execution](https://earendil-works.github.io/absurd/concepts/).

## Idempotency keys

```python
absurd_params(
    idempotency_key=f"send_report:{user_id}:{date}",
).bind(send_report).enqueue(42)
```

Whichever enqueue reaches a key first owns it; every later enqueue is swallowed and
handed the **first** task's id.

The comparison is the key alone — no task name, no arguments — so namespace it yourself
or unrelated work collides:

```python
absurd_params(idempotency_key="nightly").bind(send_report).enqueue(42)
absurd_params(idempotency_key="nightly").bind(purge_cache).enqueue()
# -> same id, and purge_cache never runs
```

- **A key is scoped to one queue.** The same key on `default` and on `reports` reserves
  independently, and both tasks run.
- **A key is held for as long as its task row exists** — freed only once the task is
  terminal and [cleanup](cleanup.md) deletes it, `cleanup_ttl` (default 30 days) after
  it finished. Not a "once per hour" window; "once until the row is swept".
- The [beat scheduler](cron-jobs.md) namespaces its own keys: a `cron:`-prefixed hash of
  the schedule name, cron expression, and slot.
