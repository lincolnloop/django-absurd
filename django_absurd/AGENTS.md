# django-absurd — integration guide

django-absurd plugs [Absurd](https://earendil-works.github.io/absurd/), a
Postgres-native workflow engine, into Django's
[Tasks framework](https://docs.djangoproject.com/en/6.0/topics/tasks/), reusing Django's
database connection and shipping Absurd's schema as Django migrations — no separate
broker.

Ships inside the installed package (`site-packages/django_absurd/AGENTS.md`), complete
on its own. Same material with navigation:
<https://lincolnloop.github.io/django-absurd/>. Runnable demos:
[`examples/`](https://github.com/lincolnloop/django-absurd/tree/main/examples) — three
single-file [nanodjango](https://github.com/radiac/nanodjango) projects, each
`docker compose up`: `web` (enqueue + result), `beat`, `pg_cron`.

## What's here

| Section                           | Go here for                                                         |
| --------------------------------- | ------------------------------------------------------------------- |
| [Requirements](#requirements)     | Python, Django, and driver versions                                 |
| [Quickstart](#quickstart)         | a minimal working setup, start to finish                            |
| [Tasks](#tasks)                   | enqueue, read a result, run later, retries, idempotency keys        |
| [Workflows](#workflows)           | checkpointed steps, durable sleep, events, heartbeat                |
| [Cron jobs](#cron-jobs)           | recurring schedules — beat or pg_cron — and operator setup          |
| [Workers](#workers)               | running a worker, every flag, runs and retries                      |
| [Cleanup](#cleanup)               | retention, scheduled cleanup, dropping every queue                  |
| [Monitoring](#monitoring)         | logging, querying queue state, the admin                            |
| [Testing](#testing)               | the `dj_absurd` fixture, durable time, automatic cleanup            |
| [Configuration](#configuration)   | every setting and `OPTIONS` key, `check` IDs, exception types       |
| [Database setup](#database-setup) | the privilege `migrate` needs, adopting an existing Absurd database |

## Requirements

- **Python 3.12+**, **Django 6.0+**.
- **PostgreSQL through the psycopg (v3) Django backend** —
  `django.db.backends.postgresql` with psycopg3 installed. The SDK reuses Django's
  connection, so psycopg2 will not work; the package asserts this at runtime.

## Quickstart

```python
INSTALLED_APPS = [
    # ...
    "django_absurd",
]

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
    },
}
```

```bash
python manage.py migrate         # install Absurd's schema, provision declared queues
python manage.py absurd_worker   # run a worker
```

```python
from django.tasks import task


@task
def add(a: int, b: int) -> int:
    return a + b


result = add.enqueue(2, 3)  # a worker runs it; read it back with add.get_result(result.id)
```

The whole loop. `"default"` is declared for you; see [Configuration](#configuration) to
add queues, set per-queue policy, or move Absurd to another database.

## Tasks

### Enqueue

```python
from django.tasks import task


@task
def send_report(user_id: int) -> None: ...


result = send_report.enqueue(42)
```

A [`@task`](https://docs.djangoproject.com/en/6.0/topics/tasks/) lives in any importable
module: tasks resolve by import path, so no `tasks.py` is required. Tasks may be sync
(`def`) or async (`async def`); one worker runs both.

- `async def` enqueues with `await send_report.aenqueue(42)`.
- Enqueuing rides the surrounding transaction, so an `atomic()` rollback drops the task.
- Delivery is **at-least-once** — keep handlers idempotent. See
  [runs and retries](#runs-and-retries).

### Read a result

```python
result = send_report.enqueue(42)

result.refresh()  # reload status / return_value / errors from the store
result.status  # READY | RUNNING | SUCCESSFUL | FAILED
result.return_value  # available once SUCCESSFUL
result.errors  # populated once FAILED

send_report.get_result(result.id)  # fetch by id later
await send_report.aget_result(result.id)  # async variant
```

Every id has the shape `"<queue>:<uuid>"`, including `context.task_result.id` inside a
`takes_context` task — either goes straight back to `get_result`.

→ [Django: task results](https://docs.djangoproject.com/en/6.0/ref/tasks/#task-results).

### Run it later

```python
send_report.using(run_after=timezone.now() + dt.timedelta(hours=1)).enqueue(42)
```

Django's
[`run_after`](https://docs.djangoproject.com/en/6.0/ref/tasks/#django.tasks.Task.run_after)
defers one enqueue, taking a timezone-aware `datetime`. For a repeating schedule use
[Cron jobs](#cron-jobs).

- Absurd's spawn has no `available_at`, so a second row, `<your task's path>:run_after`,
  waits then enqueues yours. Both are real task rows, findable by that name.
- The id you got back keeps working: `READY` while the wrapper waits, then your task's
  own status and return value. A wrapper that cannot launch stays `READY`, no visible
  error, until it exhausts its attempts — then `FAILED`.

### Retries and spawn options

Absurd's spawn options attach through one factory, `absurd_params`, at two call sites.
Exported from the package root; its home module, `django_absurd.params`, also works.

```python
from django.tasks import task
from django_absurd import absurd_params


@task
@absurd_params(max_attempts=3)  # per-task default; apply BELOW @task
def send_report(user_id: int) -> None: ...
```

```python
absurd_params(  # per invocation
    max_attempts=5,
    retry_strategy={
        "kind": "exponential",  # "fixed" | "exponential" | "none"
        "base_seconds": 2,
        "factor": 2,
        "max_seconds": 300,
    },
).bind(send_report).enqueue(42)
```

Precedence for `max_attempts`: per-invocation → decorator →
[`OPTIONS["DEFAULT_MAX_ATTEMPTS"]`](#backend-options) (`5`).

| Field             | Where                | Default                            | What it does                                                                            |
| ----------------- | -------------------- | ---------------------------------- | --------------------------------------------------------------------------------------- |
| `max_attempts`    | decorator + per-call | `DEFAULT_MAX_ATTEMPTS` (`5`)       | Retry ceiling; `None` means retry forever                                               |
| `retry_strategy`  | decorator + per-call | `kind: "none"` — retry, no backoff | Backoff: `kind` (`fixed`/`exponential`/`none`), `base_seconds`, `factor`, `max_seconds` |
| `cancellation`    | decorator + per-call | unset — no time limit              | `max_duration`, `max_delay`, in seconds                                                 |
| `headers`         | per-call only        | unset                              | Arbitrary JSON metadata carried with the task, readable as `context.headers`            |
| `idempotency_key` | per-call only        | unset — no deduping                | Dedupe within a queue — see [Idempotency keys](#idempotency-keys)                       |

- **Backoff defaults, once you name a `kind`:** `fixed` waits `base_seconds` (`60`);
  `exponential` waits `base_seconds` (`30`) × `factor` (`2`) ^ (attempt − 1), uncapped
  unless you set `max_seconds`.
- `headers` and `idempotency_key` on the decorator form are an error, statically and at
  runtime. Field types come from `absurd_sdk` (`RetryStrategy`, `CancellationPolicy`,
  `JsonObject`).
- `bind` returns an ordinary `Task`, so `aenqueue`, `call`, `get_result`, and `using`
  all keep working through it.
- Django's own options — routing (`.using(queue_name=...)`), `backend` — stay on
  [`.using()`](https://docs.djangoproject.com/en/6.0/ref/tasks/#django.tasks.Task.using),
  never on `absurd_params`. They compose in either order.
- `max_attempts=None` means retry forever, and only an explicit `None` does: omitting it
  fills in the default instead. Absurd stores a NULL ceiling and retries while it is
  NULL.
- On a non-Absurd backend the params are inert, with one `WARNING` per task naming the
  backend it ran on.
- Backend capabilities: result retrieval, async tasks, and deferred enqueue are
  supported; priority is not.

→
[Absurd: retries and durable execution](https://earendil-works.github.io/absurd/concepts/).

### Idempotency keys

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
# -> the same id, and purge_cache never runs
```

- **Scoped to one queue.** The same key on `default` and on `reports` reserves
  independently, and both run.
- **Held as long as the task row exists** — freed only once the task is terminal and
  [cleanup](#cleanup) deletes it, `cleanup_ttl` (default 30 days) after it finished. A
  pending, running, or sleeping task holds its key indefinitely. Not "once per hour";
  "once until the row is swept".
- The [beat scheduler](#application-side-beat) namespaces its own: a `cron:`-prefixed
  SHA-256 of the schedule name, expression, and slot time.

→
[Absurd: idempotency keys](https://earendil-works.github.io/absurd/concepts/#idempotency-keys).

## Workflows

Break a task into checkpointed **steps**, **sleep** between them, and suspend until a
named **event** arrives, so a retry or resume never redoes completed work.

→ [Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/).

Reach the primitives with an accessor called **inside** a running task. Pick by task
kind; each returns one concrete, fully-typed context — no cast, no union to narrow:

- **Sync task → `get_absurd_context()`** returns `django_absurd.AbsurdTaskContext`,
  mirroring the SDK's sync signatures, plus `run_step`.
- **Async task → `aget_absurd_context()`** returns
  `django_absurd.AsyncAbsurdTaskContext`, whose methods are awaited. `.absurd_ctx`
  reaches the raw SDK context for anything the wrapper doesn't mirror.

Neither needs Django's own
[`takes_context`](https://docs.djangoproject.com/en/6.0/ref/tasks/#task-context) — add
that only for `context.task_result` or `.attempt`. Outside a running Absurd task, either
accessor raises `RuntimeError`.

### Steps

```python
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    context.step("ship", lambda: ship(order_id))
```

`step(name, fn)` runs `fn()`, persists the result as a checkpoint, and skips it on
replay.

- **Step names and call order must be stable across replays** — Absurd finds checkpoints
  by them. Inserting, removing, or reordering a step or sleep corrupts replay; retire
  the task and add a new one instead.
- Results go through `json.dumps`: no sets, custom classes, or `datetime`, and a `tuple`
  comes back a `list`.
- On the async context, `fn` must return an awaitable — an `async def`; a sync lambda
  raises `TypeError`:

  ```python
  @task
  async def process_order(order_id: int) -> None:
      context = aget_absurd_context()

      async def charge():
          return await charge_card(order_id)

      await context.step("charge", charge)
  ```

→
[Absurd: Concepts — Steps (Checkpoints)](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints).

#### `run_step`

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

Wraps `step` where a lambda is awkward. Sync only.

#### Long steps

```python
def process():
    for row in big_result_set:
        process_row(row)
        context.heartbeat()  # extend the claim


context.step("process", process)
```

A run must make progress within `claim_timeout` seconds (default `120`) or it is
re-claimed and replayed from its last checkpoint. Keep steps short, or heartbeat;
`heartbeat(seconds)` extends by a set amount.

### Sleep

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    context.sleep_for("cooldown", 5)  # suspend for 5 seconds
    context.step("ship", lambda: ship(order_id))
```

The worker wakes and resumes the task — no external scheduler.
`sleep_until("wake-up", wake_at)` does the same against a fixed moment.

- Sleeps are checkpointed steps; their names share the namespace and counter with
  `step`, so they must be stable across replays too.
- A wake-up is **not a retry**: the original run is re-claimed and `attempt` does not
  increment.
- `sleep_until` takes a timezone-aware `datetime` or a Unix timestamp (`int`/`float`); a
  naive `datetime` raises against Absurd's timezone-aware clock.

→ [Absurd: Concepts — Sleep](https://earendil-works.github.io/absurd/concepts/#sleep).

### Events

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    payload = context.await_event(f"warehouse.packed:{order_id}")
    context.step("ship", lambda: ship(order_id, payload))
```

`await_event(name, step_name=None, timeout=None)` suspends the task until a named event
arrives and returns its JSON payload. `emit_event(name, payload=None)` emits one on the
task's own queue, replay-safe — a re-emit after a retry is a no-op.

- **First emit per name wins** and the payload is immutable, so a business-keyed name
  like `"warehouse.packed:order-42"` targets exactly one waiter.
- **Events are queue-scoped.** One emitted on queue X only wakes a waiter on queue X.
- An event emitted long before its `await_event` can be swept by the queue's
  `cleanup_ttl` first, and the waiter never wakes. Keep the TTL generous relative to how
  long a waiter might sleep.

→ [Absurd: Concepts — Events](https://earendil-works.github.io/absurd/concepts/#events).

#### Emit from a view

```python
from django.http import HttpResponse

from django_absurd import emit_event


def warehouse_webhook(request, order):
    emit_event(f"warehouse.packed:{order}", {"tracking": request.POST["tracking"]},
               queue="default")
    return HttpResponse(status=204)
```

`context.emit_event` only reaches code inside a task. The real signal that wakes a
waiter — a webhook, a view, an API handler — is ordinary Django code, so it uses the
top-level `django_absurd.emit_event`.

- `queue` must name the queue the waiting task runs on — a queue name, not a database
  alias.
- An undeclared queue raises `QueueNotDeclaredError`; a declared but unprovisioned one
  raises `QueueNotProvisionedError` naming `manage.py absurd_sync_queues`. See
  [Exceptions](#exceptions).
- Sync — wrap it in `sync_to_async` from an async view.

#### Timeout

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
- **Uncaught, it fails the run**, which retries and re-waits the full `timeout` on each
  attempt until `max_attempts`. Catch it for a one-shot timeout.

### Context API

All of it is on both contexts; on the async one, `await` the methods. `headers` is a
property, `run_step` sync-only.

| Method / property                                       | What it does                                              |
| ------------------------------------------------------- | --------------------------------------------------------- |
| `step(name, fn)`                                        | Run `fn()`, checkpoint the result; skip it on replay      |
| `sleep_for(step_name, duration)`                        | Suspend the task for `duration` seconds                   |
| `sleep_until(step_name, wake_at)`                       | Suspend until a `datetime` or Unix timestamp              |
| `await_event(event_name, step_name=None, timeout=None)` | Suspend until the named event arrives; return its payload |
| `emit_event(event_name, payload=None)`                  | Emit an event on the task's own queue (replay-safe)       |
| `heartbeat(seconds=None)`                               | Extend the claim timeout, keeping the run alive           |
| `headers`                                               | Read-only mapping of the headers passed at enqueue time   |
| `run_step([name])`                                      | Decorator wrapping `step`; derives the name from `fn`     |

- No `await_task_result`: the SDK's version polls and heartbeats inside a step rather
  than suspending, and is cross-queue only. Use Django's
  [`get_result()` / `aget_result()`](#read-a-result) for a child task's result.
- Checkpoints and waits are rows like any other — query them through
  [the models](#query-queue-state), or read them off a task in the admin.

### Gotchas

- **Never catch-all inside a task.** Absurd suspends and cancels runs through
  control-flow exceptions raised inside `step` / `sleep_for` / `sleep_until` /
  `await_event`. A bare `except:` or `except Exception:` around a durable call swallows
  them and silently breaks suspension.
- **Effectively-once, not exactly-once.** A step's result is persisted after `fn`
  returns, on a separate connection. A crash in that window re-runs the step, so keep
  side effects idempotent — use [`idempotency_key`](#idempotency-keys) on downstream
  enqueues, or make writes upserts.
- **Absurd backend only.** Under any other Django task backend the Absurd runtime
  context is never set, so the accessors raise `RuntimeError`.

## Cron jobs

Run tasks on a recurring cadence. **Pick one scheduler** — application-side
[beat](#application-side-beat), or Postgres-side [pg_cron](#postgres-side-pg_cron),
selected by whether `"django_absurd.pg_cron"` is in `INSTALLED_APPS`. Both read the same
`SCHEDULE`; installing the pg_cron app makes `absurd_beat` and `absurd_worker --beat`
raise `CommandError`.

→ [Absurd's cron patterns](https://earendil-works.github.io/absurd/patterns/cron/).

### Declare a schedule

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.send_report",  # dotted path to a @task
                    "cron": "0 2 * * *",  # 2am daily
                },
                "hourly-cleanup": {
                    "task": "myapp.tasks.cleanup",
                    "cron": "0 * * * *",
                    "queue": "low-priority",  # optional; must be declared
                    "args": [30],  # optional
                    "kwargs": {"dry_run": False},  # optional
                },
            },
        },
    },
}
```

| Key               | Required | Description                                                                           |
| ----------------- | -------- | ------------------------------------------------------------------------------------- |
| `task`            | yes      | Dotted import path to a [`@task`](#enqueue) function                                  |
| `cron`            | yes      | Cron expression; the grammar differs per scheduler, below                             |
| `queue`           | no       | Queue to enqueue on; defaults to the backend's. Must be [declared](#declaring-queues) |
| `args` / `kwargs` | no       | Passed to the task on each firing. `args` a JSON array, `kwargs` a JSON object        |

`manage.py check` validates every entry and reports `absurd.E007` for an unimportable or
non-`@task` path, an invalid cron expression, unknown keys, `args`/`kwargs` that are not
JSON-serializable or are the wrong shape, an undeclared `queue`, or (pg_cron only) a
schedule name outside `[A-Za-z0-9_-]`. Fix everything it reports before relying on the
schedule in production.

- **Set `queue` explicitly on a non-default backend.** An entry without one falls back
  to the task function's own `queue_name`, which may not be declared for that backend.
  Under pg_cron `absurd.E007` validates that fallback; under beat only an explicit
  `queue` is checked.

### Application-side: beat

```bash
python manage.py absurd_beat            # standalone process
python manage.py absurd_worker --beat   # or co-located with a worker
```

Beat evaluates cron in-process and enqueues each task when its slot comes due; a
[worker](#workers) then runs it like any other.

- **Run exactly one beat process.** No leader election, and two beats each fire every
  slot. Per-slot idempotency — a `cron:`-prefixed hash of name, expression, and slot
  time, following the
  [Absurd cron pattern](https://earendil-works.github.io/absurd/patterns/cron/) —
  collapses duplicates from a brief overlap or a restart re-firing a slot, so each slot
  fires at most once. It does not replace single-instance supervision.
- **Never backfills.** A slot that passes while beat is down is skipped; the next one
  proceeds on schedule.
- Grammar is [croniter](https://pypi.org/project/croniter/): standard 5-field
  `min hour dom mon dow`, or 6-field with a leading seconds column for sub-minute
  cadence (`"*/30 * * * * *"` is every 30s). Each slot enqueues a task, so size the
  cadence to what your workers can keep up with.
- Expressions are evaluated in Django's
  [`TIME_ZONE`](https://docs.djangoproject.com/en/6.0/ref/settings/#time-zone).

### Postgres-side: pg_cron

```python
INSTALLED_APPS = [
    # ...
    "django_absurd",
    "django_absurd.pg_cron",  # must come AFTER "django_absurd"
]
```

```bash
python manage.py migrate
```

Postgres fires the schedule directly — no beat process. `migrate` reconciles `SCHEDULE`
into [pg_cron](https://github.com/citusdata/pg_cron) jobs on `post_migrate`, and your
existing workers run the tasks; a settings-only change needs no new migration file, so
"migrate on deploy" covers it. The extension itself is one-time
[operator setup](#operator-setup).

- **Grammar is pg_cron's own**, validated in Python by `manage.py check` for settings
  schedules and at save time for admin ones: a 5-field cron, the interval form
  `<n> seconds` (1-59, singular or plural, case-insensitive), or one of `@hourly`,
  `@daily`, `@weekly`, `@monthly`, `@yearly`/`@annually`, `@midnight`.
- **Beat's 6-field form and the `#` nth-weekday token are refused even though pg_cron
  accepts them.** Its parser reads five fields and treats the rest as the command, so
  `"*/30 * * * * *"` would silently schedule `"*/30 * * * *"` — a cadence you did not
  write, reported as valid. `@reboot` and `@restart` are refused too: neither is a
  recurring cadence.
- **Timezone is the `cron.timezone` GUC, default GMT** — not Django's `TIME_ZONE`. Set
  it to match if yours is not UTC.
- **To stop a job, remove it from `SCHEDULE`.** Every reconcile re-arms settings-owned
  jobs, so disabling one directly in `cron.job` does not survive the next deploy.
- `absurd.W003` if the app is ordered before `"django_absurd"`, which would run its
  `post_migrate` reconcile before queue provisioning. `absurd.E013` if the app is
  installed with no `AbsurdBackend` configured, so schedules would save and never fire.

pg_cron is **cluster-wide**: only the database named by
[`cron.database_name`](https://github.com/citusdata/pg_cron#configuring-pg_cron) may
hold it, and yours probably is not it. django-absurd never installs the extension on the
Absurd database and never touches `cron.*` there — it discovers that central database
(`current_setting('cron.database_name')`) and schedules each job
[cross-database](https://github.com/citusdata/pg_cron#cross-database-scheduling).
Nothing to configure.

#### Reconcile without migrating

```bash
python manage.py absurd_sync_crons
```

The backstop for pipelines that skip `migrate` when no migration files changed. Reports
synced and pruned counts; exits non-zero on error.

- **Always connect as the same role.** pg_cron keys jobs on `(jobname, username)` and
  runs each as its scheduling role, so mixing roles duplicates jobs and breaks pruning.

#### Author schedules in the admin

Each schedule is materialised as a `ScheduledTask` row. Settings-declared rows
(`Source.SETTINGS`) are **read-only** — `SCHEDULE` is their source of truth. Admins
author their own (`Source.ADMIN`) from `name`, `task` and `cron`; the remaining spawn
options (`queue`, `max_attempts`, retry strategy, cancellation, `headers`,
`idempotency_key`) resolve from the task's `@task` / `@absurd_params` decorators, and
the row is created **disabled** so it does not fire until someone enables it. Saving or
deleting an enabled row immediately (un)schedules its pg_cron job.

- `name` is immutable — it forms the job identity — and the resolved options are frozen
  at create, so later decorator edits do not change existing rows. The cron expression
  is validated at save time against the grammar `check` applies to settings schedules,
  with no database round-trip. `max_attempts` defaults to `5` and must be `>= 1`;
  clearing it stores NULL, which Absurd treats as **retry forever**.
- The row is the source of truth: editing `args`, `kwargs`, or options takes effect on
  the next firing without touching `cron.job`, and any write that persists the row keeps
  pg_cron in step. Writes that bypass `.save()` — a data migration, `bulk_create`,
  `QuerySet.update`, raw SQL — emit on the next reconcile.
- `loaddata` bypasses the router, so pass `--database=<alias>` when Absurd is on a
  non-default database; a write forced onto a different database raises
  `NotImplementedError`, since schedules live only on the Absurd database. A settings
  schedule and an admin schedule may share a name — distinct, source-namespaced jobs.

#### Test databases

```python
"OPTIONS": {"PG_CRON_ON_TEST_DB": True}  # opt in; off by default
```

Every `cron.*` write is inert on a test database or during a test run, detected
automatically. A plain test database carries no `pg_cron` extension, so the write would
fail outright — and where it would not, pg_cron's launcher runs independently of pytest
and Django, so a leftover schedule fires for real, on cadence, against test data.

| Option                      | Default | Effect                                                                                      |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------- |
| `PG_CRON_ON_TEST_DB`        | `False` | The opt-in. Without it every write no-ops and `absurd_sync_crons` refuses to run            |
| `SYNC_SCHEDULES_ON_MIGRATE` | `True`  | `migrate`'s automatic reconcile against a real database                                     |
| `SYNC_SCHEDULES_ON_TEST_DB` | `False` | The same, against a test database. Setting it without `PG_CRON_ON_TEST_DB` is `absurd.E011` |

`absurd_sync_crons` is never gated by either sync key — it is explicit, not a side
effect of `migrate` — but `PG_CRON_ON_TEST_DB` gates it like every other `cron.*` write,
and it refuses with a `CommandError` rather than silently doing nothing. See
[getting a `SCHEDULE` into pg_cron for a test](#getting-a-schedule-into-pg_cron-for-a-test).

#### Uninstall

```bash
python manage.py absurd_sync_crons --teardown   # --noinput (alias --no-input) in automation
```

Run this **before** removing `"django_absurd.pg_cron"` from `INSTALLED_APPS` or
switching back to beat. Removing the app stops the reconcile but leaves the jobs firing,
with nothing left to clean them up.

- It unschedules every owned job for the backend and deletes its `ScheduledTask` row,
  **admin-authored ones included** — otherwise the next `migrate` re-emits a job for
  every surviving admin row and resurrects what teardown just killed. It prompts for
  confirmation because it destroys admin-authored schedules.
- Migrate-time teardown (a backend switching off pg_cron) is narrower: settings jobs and
  rows only, never admin ones. Not a substitute for this command.

#### Operator setup

One-time, on the **central** database named by `cron.database_name` — not necessarily
the Absurd one. A migration cannot do any of it.

- **pg_cron ≥ 1.4.** `cron.schedule_in_database`'s full signature and `cron.alter_job`,
  both used on every reconcile, landed in 1.4.
- **`shared_preload_libraries = pg_cron`** in `postgresql.conf`, which needs a server
  restart.
- **`CREATE EXTENSION pg_cron`**, run once on that central database.
- **Grants** for the scheduling role — the role `migrate` / `absurd_sync_crons` connects
  as — on that same central database. Skip them entirely if that role owns the extension
  (it ran `CREATE EXTENSION` itself, or is a superuser), since an owner already has
  execute rights.

  ```sql
  GRANT USAGE ON SCHEMA cron TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.schedule_in_database(text, text, text, text, text, boolean)
      TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.alter_job(bigint, text, text, text, text, boolean)
      TO <scheduling_role>;
  ```

  The `alter_job` grant is required, not optional: `schedule_in_database`'s `active`
  argument only takes effect when it first creates a job, so disabling an
  already-scheduled job needs an explicit `alter_job` call.

- Managed Postgres (RDS, Cloud SQL, Azure) exposes these as parameter-group flags.
- `python manage.py check --database default` reports `absurd.E012` when that central
  database is unreachable or missing the extension. A deploy-time check: it runs on
  `migrate` and any `check --database` invocation, never on a plain DB-free `check`, and
  stays quiet under a test suite.
- **Also worth scheduling: a
  [`cron.job_run_details`](https://github.com/citusdata/pg_cron#viewing-job-run-details)
  purge.** It is the only place fire-time failures show up, and it grows unbounded.

Stock `postgres` images ship no pg_cron, so build it in for local work:

```dockerfile
# pg_cron.Dockerfile — Debian base; Alpine has no pg_cron package
FROM postgres:18
RUN apt-get update && apt-get install -y postgresql-18-cron
```

```yaml
# compose.yaml — these are server flags, so they cannot live in the image
services:
  db:
    build:
      dockerfile: pg_cron.Dockerfile
    command: postgres -c shared_preload_libraries=pg_cron
    environment: { POSTGRES_PASSWORD: postgres }
```

`cron.database_name` defaults to `postgres`; pass `-c cron.database_name=<db>` to point
it elsewhere. Either way, run the `CREATE EXTENSION` and grants above once against
whichever database it names — an initdb script (`\connect postgres` then
`CREATE EXTENSION`) does it.

## Workers

```bash
python manage.py absurd_worker                     # consumes the "default" queue
python manage.py absurd_worker --queue reports --concurrency 4
```

A single worker runs **both** sync and async tasks: `async def` on an event loop (true
concurrency for I/O-bound work), sync `def` in a thread pool. On start it reconciles
every declared queue — creating missing ones, applying declared policy changes — and
rebuilds the admin views to reflect the whole catalog, not just the served queue,
reporting to stdout. Then it polls until `SIGINT`/`SIGTERM`.

| Flag              | Default         | What it does                                          |
| ----------------- | --------------- | ----------------------------------------------------- |
| `--queue`         | `default`       | Queue to consume                                      |
| `--concurrency`   | `1`             | Max tasks in flight; also the sync thread-pool size   |
| `--claim-timeout` | `120`           | Seconds before a claimed task returns to the queue    |
| `--poll-interval` | `0.25`          | Seconds between polls                                 |
| `--batch-size`    | `--concurrency` | Max tasks claimed per poll cycle                      |
| `--worker-id`     | `<host>:<pid>`  | Identifier recorded on each claim                     |
| `--beat`          | off             | Also run the [beat scheduler](#application-side-beat) |

- **One queue per worker.** `--queue` takes a single name; run a process per queue.
- A concurrency slot is refilled as soon as it frees rather than waiting for the whole
  batch, so one slow task does not idle the others.
- Run **exactly one** `--beat` across the fleet; there is no leader election.
- A stop signal stops claiming and lets in-flight tasks finish before the worker exits.

→
[Absurd: Concepts — Workers](https://earendil-works.github.io/absurd/concepts/#workers).

### Runs and retries

Each attempt at a task is a **run**. A failed task retries up to its
[`max_attempts`](#retries-and-spawn-options), and work wrapped in a [step](#steps) is
checkpointed, so retries never redo completed work. A run that makes no progress within
`--claim-timeout` is re-claimed and replayed from its last checkpoint.

Delivery is **at-least-once**: a task may run more than once — say after a crash between
a handler committing and Absurd's bookkeeping — so keep handlers idempotent and use
[`idempotency_key`](#idempotency-keys) where it helps.

→
[Absurd: Concepts — Retries](https://earendil-works.github.io/absurd/concepts/#retries).

### What `migrate` installs

```bash
python manage.py migrate
```

Absurd's schema ships as a Django migration, and `post_migrate` provisions the declared
queues and rebuilds the admin views. Declared queues are additionally created on worker
start, by `absurd_sync_queues`, and on first enqueue; only declared queues are ever
created, and an undeclared name is rejected. The SQL comes from the pinned Absurd
version and is never fetched at migrate time.

→ [Absurd: database setup](https://earendil-works.github.io/absurd/database/).

## Cleanup

Task rows accumulate in Postgres unless you prune them. Cleanup deletes **terminal**
rows — completed, failed, cancelled — older than a queue's TTL, up to its batch limit.
Running and pending tasks are never touched.

```bash
python manage.py absurd_cleanup            # every queue
python manage.py absurd_cleanup reports    # only the named queue(s)
# default: 12 tasks, 0 events deleted
```

```python
from django_absurd.cleanup import cleanup_queues

cleanup_queues()  # every declared queue
cleanup_queues(["reports", "emails"])
# → [{"queue_name": "default", "tasks_deleted": 12, "events_deleted": 0}]
```

An unknown queue name raises the raw database error — cleanup is a maintenance
operation, so nothing masks it.

### Schedule recurring cleanup

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "CLEANUP": {"schedule": "0 3 * * *"},  # 3am daily
        },
    },
}
```

Runs on cadence under **either** [scheduler](#cron-jobs), no user code: beat in-process,
pg_cron through Absurd's own `absurd.cleanup_all_queues`. django-absurd owns that job
outright — scheduling it from `OPTIONS["CLEANUP"]`, removing it otherwise, including at
migrate teardown or a scheduler flip.

- `absurd.E010` for a malformed `CLEANUP`, including a cron expression the configured
  scheduler cannot run; `check` validates both grammars.
- **Drive cleanup one way only** — `OPTIONS["CLEANUP"]` **or** `absurdctl cron`, never
  both. Absurd's own maintenance scheduler (`absurd.enable_cron`, which
  `absurdctl cron --enable <queue>` drives) is a separate mechanism creating per-queue
  jobs that django-absurd neither uses nor manages. It cannot see or remove them, so
  they survive every teardown and fire alongside its own.

### Retention knobs

```python
"OPTIONS": {"QUEUES": {
    "reports": {"cleanup_ttl": "7 days", "cleanup_limit": 1000},
}}
```

Per-queue policy, set where you [declare the queue](#declaring-queues):

| Option          | What it controls                                                                                   |
| --------------- | -------------------------------------------------------------------------------------------------- |
| `cleanup_ttl`   | Minimum age a terminal task must reach before deletion (default 30 days)                           |
| `cleanup_limit` | Max terminal rows deleted per queue per run, applied separately to task and event rows (batch cap) |

→ [Absurd: cleanup](https://earendil-works.github.io/absurd/cleanup/) ·
[Absurd: storage](https://earendil-works.github.io/absurd/storage/).

### Reset — drop every queue

```bash
python manage.py absurd_flush            # prompts, then drops on 'yes'
python manage.py absurd_flush --noinput  # drops without prompting
```

**Destructive: this deletes all task history.** It removes every queue — its per-queue
tables and registry entry — along with all tasks, runs, and events in them. It does not
uninstall Absurd: the schema, migrations, and functions stay, so you never re-`migrate`,
only re-provision the queues with `migrate`, `absurd_sync_queues`, or by starting a
worker.

- Scheduled jobs survive the flush and **error on each fire** until the queues exist
  again, so re-provision promptly. The `OPTIONS["CLEANUP"]` job is the exception: it
  survives and runs harmlessly, finding no eligible rows.
- Per-queue Absurd maintenance jobs from `absurdctl cron --enable <queue>` are dropped
  with their queue.

## Monitoring

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        # everything from this package...
        "django_absurd": {"handlers": ["console"], "level": "INFO"},
        # ...or quiet one part of it
        "django_absurd.scheduler": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
```

Two loggers, configured like any other in Django's
[`LOGGING`](https://docs.djangoproject.com/en/6.0/topics/logging/#configuring-logging):

- **`django.tasks`** — Django's own task lifecycle, from the signals `AbsurdBackend`
  emits. Portable across backends.
- **`django_absurd`** — what Absurd did: attempts, durations, worker and beat lifecycle,
  steps, replays, sleeps, event waits. One child per module, each levellable on its own
  — `django_absurd.scheduler` is the beat, `django_absurd.context` the durable
  primitives.

`absurd_worker` and `absurd_beat` attach a `StreamHandler` at `INFO` so a fresh project
is not silent. Naming `django_absurd` or one of its children in `LOGGING` stops that;
naming only `root` adds no handler but still raises the level, so a `WARNING` root does
not swallow them.

**Neither logger is the complete record. Postgres is** — the
[stored result](#read-a-result) and the queue-state models below.

### Query queue state

```python
from django_absurd.models import Task, Run, Checkpoint, Event, Wait, Queue

Task.objects.filter(queue="reports", state="failed")
Task.objects.get(queue="reports", task_id=task_id)
```

`Task`, `Run`, `Checkpoint`, `Event`, and `Wait` are ordinary chainable Django models —
each spanning every queue through a `UNION ALL` over the per-queue tables, carrying a
synthesized `queue` column. `Queue` is the queue catalog, keyed by `queue_name`.

- They are **read-only**: `save()`/`delete()` raise `QueueReadOnlyError`.
- **Filter by `queue=` whenever you can.** The views carry no cross-queue index, so
  `queue=` prunes to a single per-queue table while an unfiltered query — ordering by
  `enqueue_at`, or filtering only on `state` — scans every queue's table.
- They are backed by Postgres views rebuilt by `migrate`, worker start, and
  `absurd_sync_queues`. A queue that appears only afterwards — declared late and reached
  by an enqueue before the next provisioning step — is absent from results until then.
  Dropping a queue removes its view.

### The admin

With `django.contrib.admin` installed, django-absurd registers read-only pages for the
models above. No configuration. Turn them off with [`ENABLE_ADMIN`](#backend-options),
or register elsewhere with `ADMIN_SITE`.

- A queue created only by an enqueue, with no worker started and no sync run, is not yet
  in the views, so its tasks do not appear — run `absurd_sync_queues`.
- **Non-default `DATABASE`:** the synthesized models read from the Absurd database, but
  Django's own `LogEntry`, session, and `ContentType` tables must still exist in
  `"default"` — run `migrate` on it too.

→ What each page shows, with screenshots:
<https://lincolnloop.github.io/django-absurd/admin/>.

## Testing

```python
import pytest

pytestmark = pytest.mark.django_db(transaction=True)


def test_add_completes(dj_absurd):
    add.enqueue(2, 3)

    (run,) = dj_absurd.drain()

    assert run.state == "completed"
    assert run.result == 5
```

Installing django-absurd registers a
[`pytest11` entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others)
automatically — nothing to configure. The plugin builds on
[pytest-django](https://pytest-django.readthedocs.io/); install that in your test
environment.

- **`transaction=True` is required.** Absurd works on its own connection, so under a
  plain [`db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#db) test the
  enqueued row is invisible to it; `drain`, `emit`, and `get_result` detect the open
  transaction and raise rather than silently no-op.
- **Works unchanged from `async def` tests** — same names, nothing to `await` on the
  fixture. Enqueue with Django's own `await add.aenqueue(2, 3)`.
- **Multi-DB: declare the Absurd alias** in that test's
  [`databases`](https://docs.djangoproject.com/en/6.0/topics/testing/tools/#django.test.TransactionTestCase.databases),
  or its committed state leaks into the next test.

### Move durable time

```python
import datetime as dt


def test_followup_sleeps_seven_days_then_completes(dj_absurd):
    with dj_absurd.freeze_time(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) as frozen_time:
        send_followup.enqueue()  # enqueue INSIDE the block

        (sleeping,) = dj_absurd.drain()
        assert sleeping.state == "sleeping"

        frozen_time.shift(dt.timedelta(days=7))

        (woken,) = dj_absurd.drain()
        assert woken.state == "completed"
        assert woken.run_id == sleeping.run_id  # the same run resumed...
        assert woken.attempt == 1  # ...so it was never a retry
```

`freeze_time(instant=None)` pins durable time for the block (`None` = real now at
entry). Its `FrozenTime` handle carries the only two movers, `move_to(datetime)` and
`shift(timedelta)`, and each moves Python's clock (through
[time-machine](https://github.com/adamchainz/time-machine)) and Postgres's
`absurd.fake_now` together.

- **Enter the block before the `enqueue()` calls whose deadlines you want to control.**
  Freezing to a past instant after rows exist leaves their deadlines in the database's
  future, so nothing is claimable until a later move passes them.
- `shift(Δ)` is absolute elapsed time, which is what a durable deadline measures.
- Blocks do not nest — two frozen instants cannot both be "now" — and a `FrozenTime`
  raises once its block has exited rather than silently re-freezing. Sequential blocks
  are fine.
- **Install [time-machine](https://github.com/adamchainz/time-machine) yourself** — a
  test dependency of your project, not bundled and not an extra. Only `freeze_time`
  imports it, lazily, raising `ImproperlyConfigured` with the install command if
  missing. A test that never freezes pays nothing.
- **Don't enqueue across a savepoint rollback.** The rollback reverts Django's session
  clock, so a later `enqueue()` stamps real time and will not look claimable.
- **A freeze does not reach [pg_cron](#postgres-side-pg_cron)**, whose launcher runs in
  another database on its own clock. Testing a schedule stays "reconcile it in, then
  inspect `cron.job`".

`FrozenTime`, `AbsurdTestRuntime` (what `dj_absurd` is typed as), `TaskSnapshot`, and
`RunSnapshot` are importable from `django_absurd.test` for annotating your own helpers.

### Fixture API

`dj_absurd` is the only fixture. All five members plus `now` work unchanged in an
`async def` test.

| Member                                      | Does                                                                       |
| ------------------------------------------- | -------------------------------------------------------------------------- |
| `freeze_time(instant=None)`                 | Context manager pinning durable time                                       |
| `now`                                       | Virtual now, timezone-aware, as Postgres itself reports it                 |
| `sync_queues()`                             | Provision every declared queue                                             |
| `drain(queue="default")`                    | Run a queue's claimable tasks to completion, returning `list[RunSnapshot]` |
| `emit(name, payload=None, queue="default")` | Deliver an event, resolving a task suspended in `await_event`              |
| `get_result(task_id, queue=...)`            | Look up one task, returning `TaskSnapshot`                                 |

**`drain()`** runs every currently-claimable task on `queue` to completion in-process —
no worker subprocess, no polling loop — one at a time, returning one `RunSnapshot` per
run executed, in claim order. It **provisions nothing**, unlike the CLI: `migrate`
leaves a test database ready, but a queue a single test declares by overriding `TASKS`
has no table, so call `sync_queues()` first or `drain()` raises
`QueueNotProvisionedError`. An undeclared queue raises `QueueNotDeclaredError`.

```python
(run,) = dj_absurd.drain()  # one RunSnapshot per run, in claim order

run.queue, run.task_id  # which task this run belongs to
run.run_id  # this run's id; the same value twice for a re-armed await_event waiter
run.task_name  # dotted task path
run.args, run.kwargs  # decoded from the enqueued params
run.attempt  # 1-based
run.state  # pending | sleeping | completed | failed | cancelled
run.result  # the return value, once completed
run.failure  # {"message": str, "name"?: str, "traceback"?: str}, once failed
```

- `pending` is claimable and not yet run; `completed` finished; `failed` raised and is
  out of retries; `cancelled` was cancelled before or during execution. **`sleeping`
  covers a durable [sleep](#sleep), an `await_event` wait, and a retry backoff alike** —
  indistinguishable from one snapshot.

**`get_result()`** returns a `TaskSnapshot` or raises `TaskNotFoundError`. Unlike
[`my_task.get_result()`](#read-a-result) it reads Absurd's own states directly —
including `sleeping`, which `TaskResult.status` cannot show — and skips the worker
round-trip.

```python
result = reports_task.enqueue()  # id is "reports:<uuid>"
snapshot = dj_absurd.get_result(result.id)  # queries the "reports" queue

snapshot.queue, snapshot.task_id  # no queue prefix on task_id
snapshot.task_name
snapshot.args, snapshot.kwargs
snapshot.state  # same vocabulary as a RunSnapshot
snapshot.attempts  # attempts CREATED, not completed
snapshot.enqueued_at  # when enqueue() ran
snapshot.result  # once completed
snapshot.failure  # None except on a terminal failure
```

- `task_id` takes a bare uuid or Django's own prefixed `TaskResult.id`. The prefix wins
  over `queue`'s default; a `queue=` that disagrees with it raises
  `TaskIdQueueMismatchError` naming both.
- **This view cannot express an in-flight retry**: `attempts` reads `2` before the
  second attempt runs, `state="sleeping"` covers a backoff as well as a durable sleep,
  and `failure` is `None` mid-backoff. Use `drain()`'s `RunSnapshot`, which reports each
  run's own state right after it executes, to tell them apart.
- **A deferred task's id names its wrapper.** A [`run_after`](#run-it-later) enqueue
  reports the `<task path>:run_after` row here; use Django's own `get_result` for your
  task's own status.

**`sync_queues()`** is the runtime counterpart of `manage.py absurd_sync_queues`. Rarely
needed, since `migrate` already provisions the declared catalog — reach for it only when
the test itself changed queue topology.

### Cleanup is automatic

pytest users do nothing: the plugin wires Absurd state cleanup into Django's own test
teardown, exact parity with how Django resets its own tables. No fixture to request, no
marker to add.

- Plain `TestCase` / `db` tests need none — the `enqueue()` rides the same uncommitted
  transaction Django
  [rolls back](https://docs.djangoproject.com/en/6.0/topics/testing/overview/#rollback-emulation).
- `transaction=True` tests commit for real, so queue state is truncated after each — and
  with `django_absurd.pg_cron` installed, its settings- and admin-authored jobs plus the
  `OPTIONS["CLEANUP"]` job are unscheduled too.
- Multi-DB: cleanup only runs when the test's `databases` includes the Absurd alias
  (respecting `"__all__"`), matching Django's own per-alias flush scoping.
- No database access means no Absurd access — `enqueue()` trips pytest-django's own
  [blocking](https://pytest-django.readthedocs.io/en/latest/database.html) like any
  query.

### Getting a `SCHEDULE` into pg_cron for a test

```python
settings.TASKS["default"]["OPTIONS"]["PG_CRON_ON_TEST_DB"] = True
call_command("absurd_sync_crons")
```

Auto-cleanup only tears down; it has no say over whether a `SCHEDULE` lands in pg_cron
in the first place, and every `cron.*` write is [inert under tests](#test-databases) by
default. `PG_CRON_ON_TEST_DB` is the opt-in; using `migrate`'s automatic reconcile
instead also needs `SYNC_SCHEDULES_ON_TEST_DB = True`. Cleanup then clears whatever
ended up in `cron.job` / `ScheduledTask`, however it got there.

### `manage.py test`

```python
from django.test.runner import DiscoverRunner

from django_absurd.test import install_absurd_cleanup


class MyTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        install_absurd_cleanup()
```

Django's own
[`DiscoverRunner`](https://docs.djangoproject.com/en/6.0/topics/testing/advanced/#django.test.runner.DiscoverRunner)
has no equivalent auto-hook — pytest is django-absurd's primary test surface. Wire the
same public hook yourself and point `TEST_RUNNER` at your subclass;
`install_absurd_cleanup()` is idempotent, so calling it where pytest's plugin already
did is a no-op.

## Configuration

Everything django-absurd reads lives under Django's
[`TASKS`](https://docs.djangoproject.com/en/6.0/topics/tasks/) setting.

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],  # optional
        "OPTIONS": {  # optional
            "DATABASE": "default",
        },
    },
}
```

### Declaring queues

Declare queues in **one** place, never both — setting both is `absurd.E002`.

```python
"QUEUES": ["default", "reports", "emails"]  # names only
```

```python
"OPTIONS": {"QUEUES": {  # names → per-queue policy
    "default": {},
    "reports": {"cleanup_ttl": "7 days"},
}}
```

The map form takes
[`absurd_sdk.CreateQueueOptions`](https://earendil-works.github.io/absurd/sdks/python/),
which is where [retention](#retention-knobs) lives. Omit both to use just `"default"`.

- Undeclared queue names are rejected, never silently created. Queue creation is
  additive: nothing ever drops a queue you removed from config.
- A queue's `storage_mode` is immutable once it exists; a declared change is reported as
  `absurd.W002` and not applied.
- `storage_mode="partitioned"` is declarable but **experimental — not tested yet**, with
  no automated partition lifecycle. Don't rely on it in production.

→ [Absurd: storage](https://earendil-works.github.io/absurd/storage/).

### Backend `OPTIONS`

All optional:

| Option                      | Default                          | What it does                                                                                                     |
| --------------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `DATABASE`                  | `"default"`                      | Which `DATABASES` alias to use                                                                                   |
| `DEFAULT_MAX_ATTEMPTS`      | `5`                              | Retry ceiling per task; an integer `>= 1`. Override per task or call — see [retries](#retries-and-spawn-options) |
| `QUEUES`                    | —                                | Map of queue name → policy, above. Mutually exclusive with the top-level list                                    |
| `CLEANUP`                   | —                                | `{"schedule": "<cron>"}` to run [cleanup](#schedule-recurring-cleanup) on cadence                                |
| `SCHEDULE`                  | —                                | Recurring task schedules — see [Cron jobs](#declare-a-schedule)                                                  |
| `SYNC_SCHEDULES_ON_MIGRATE` | `True`                           | (pg_cron) Reconcile `SCHEDULE` on `migrate` — see [test databases](#test-databases)                              |
| `SYNC_SCHEDULES_ON_TEST_DB` | `False`                          | (pg_cron) Allow that migrate-time sync on a test database                                                        |
| `PG_CRON_ON_TEST_DB`        | `False`                          | (pg_cron) Opt in to real `cron.*` writes on a test database or during a test run                                 |
| `ENABLE_ADMIN`              | `True`                           | Register the read-only Absurd models in [the admin](#the-admin)                                                  |
| `ADMIN_SITE`                | `("django.contrib.admin.site",)` | Dotted paths to the `AdminSite`(s) to register on                                                                |

### Non-default database

```python
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]
```

Required only when `DATABASE` names an alias other than `"default"` (`absurd.E005`
otherwise). The
[router](https://docs.djangoproject.com/en/6.0/topics/db/multi-db/#using-routers) sends
django-absurd's schema and queries there. One `AbsurdBackend` per project is supported.

### Validate it

```bash
python manage.py check django_absurd
```

Each check states the problem and hints the resolution, so fix what it points at rather
than silencing it.

| ID            | Means                                                                                                                 |
| ------------- | --------------------------------------------------------------------------------------------------------------------- |
| `absurd.E001` | Backend or database misconfiguration                                                                                  |
| `absurd.E002` | `QUEUES` declared in both the top level and `OPTIONS`                                                                 |
| `absurd.E003` | Invalid per-queue policy options                                                                                      |
| `absurd.E004` | More than one Absurd backend configured; exactly one per project is supported                                         |
| `absurd.E005` | `AbsurdRouter` missing from `DATABASE_ROUTERS`                                                                        |
| `absurd.E006` | `ENABLE_ADMIN` is not a bool, or `ADMIN_SITE` does not resolve to `AdminSite` instances                               |
| `absurd.E007` | Invalid `SCHEDULE` entry — see [Declare a schedule](#declare-a-schedule)                                              |
| `absurd.E009` | `DEFAULT_MAX_ATTEMPTS` is not an integer `>= 1`                                                                       |
| `absurd.E010` | Invalid `CLEANUP` — see [scheduled cleanup](#schedule-recurring-cleanup)                                              |
| `absurd.E011` | `SYNC_SCHEDULES_ON_TEST_DB` is `True` without `PG_CRON_ON_TEST_DB`                                                    |
| `absurd.E012` | The central `cron.database_name` database is unreachable or missing `pg_cron` — see [operator setup](#operator-setup) |
| `absurd.E013` | `"django_absurd.pg_cron"` installed with no `AbsurdBackend` configured                                                |
| `absurd.E014` | `OPTIONS["QUEUES"]` is not a mapping of queue name to policy options                                                  |
| `absurd.W002` | (Warning) A queue's declared `storage_mode` differs from the database                                                 |
| `absurd.W003` | (Warning) `django_absurd.pg_cron` ordered before `django_absurd` in `INSTALLED_APPS`                                  |

### Exceptions

```python
from django_absurd.exceptions import DjangoAbsurdError

try:
    emit_event("warehouse.packed:42", queue="reports")
except DjangoAbsurdError:
    ...
```

| Type                        | Raised when                                                                                         |
| --------------------------- | --------------------------------------------------------------------------------------------------- |
| `SchemaNotInstalledError`   | The Absurd Postgres schema itself isn't installed — run `manage.py migrate`                         |
| `QueueNotDeclaredError`     | A queue name matches no queue declared for the backend                                              |
| `QueueNotProvisionedError`  | A queue is declared but its Absurd table isn't provisioned yet — run `manage.py absurd_sync_queues` |
| `BackendNotConfiguredError` | No `AbsurdBackend`, or more than one, is configured in `TASKS`                                      |
| `QueueReadOnlyError`        | `.save()`/`.delete()` on one of the [read-only models](#query-queue-state)                          |
| `ViewNotProvisionedError`   | One of those views hits a missing relation because a queue was never provisioned                    |
| `TaskIdQueueMismatchError`  | `dj_absurd.get_result` got a `"queue:uuid"` id and a `queue=` naming different queues               |
| `TaskNotFoundError`         | `dj_absurd.get_result` found no task by that id on that queue                                       |

- `enqueue` raises `QueueNotDeclaredError` only when the backend's `QUEUES` is empty or
  unset; with `QUEUES` configured, a typo'd name is rejected earlier as Django's own
  `InvalidTask`.
- Every `absurd_*` management command inherits `AbsurdCommand` (or its
  `AbsurdReportCommand` subclass) and turns a configuration failure —
  `ImproperlyConfigured` or any `DjangoAbsurdError` — into a clean `CommandError`;
  `--traceback` still shows the original chain.
- The hierarchy is not total. Catch `DjangoAbsurdError` for django-absurd's own typed
  errors; other failures — config validation, clock misuse — still raise plain
  `ImproperlyConfigured` / `RuntimeError` / `TypeError`.

## Database setup

```bash
python manage.py migrate
```

`migrate` runs `CREATE SCHEMA IF NOT EXISTS absurd` and needs nothing beyond the rights
to do that — **no extension, so no superuser and no managed-Postgres allow-list entry.**
It does need `GRANT CREATE ON DATABASE <db>`: `CREATE SCHEMA IF NOT EXISTS` checks that
privilege _before_ it checks whether the schema exists, so pre-creating `absurd`
yourself does not avoid the grant. The schema name is fixed.

- Choosing [pg_cron](#postgres-side-pg_cron) gives that privileged role more to do,
  once, on the central `cron.database_name` database — see
  [operator setup](#operator-setup).
- What the migration installs and provisions:
  [What `migrate` installs](#what-migrate-installs).

### Adopting an existing Absurd database

```bash
python manage.py migrate --fake django_absurd
```

If the target database already runs Absurd with its schema managed outside Django,
faking the migration records it as applied without re-running the DDL.

**Use extreme caution:** faking tells Django the schema is present without checking it.
Only do this when the existing `absurd` schema exactly matches the version this package
targets (`django_absurd.ABSURD_SCHEMA_VERSION`) — a mismatch causes runtime failures
Django cannot detect. Verify the versions line up first.

---

Alpha software: APIs may change between versions.
