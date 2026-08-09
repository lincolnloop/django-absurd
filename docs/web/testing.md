---
icon: lucide/flask-conical
---

# Testing

django-absurd ships a [pytest](https://docs.pytest.org/) plugin, registered
automatically on install via a
[`pytest11` entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others).
It builds on [pytest-django](https://pytest-django.readthedocs.io/) — install that
alongside django-absurd.

## Run a task in a test

```python
import pytest

pytestmark = pytest.mark.django_db(transaction=True)


def test_add_completes(dj_absurd):
    add.enqueue(2, 3)

    (run,) = dj_absurd.drain()

    assert run.state == "completed"
    assert run.result == 5
    assert run.task_name == "myapp.tasks.add"
    assert run.args == [2, 3]
    assert run.attempt == 1
```

`dj_absurd` is the only fixture. `drain()` runs every claimable task to completion
in-process — no [worker](workers.md) subprocess, no polling loop — and returns one
`RunSnapshot` per run.

- **`transaction=True` is required.** Absurd works on its own connection, so under a
  plain [`db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#db) test the
  enqueued row is invisible to it. `drain`, `emit`, and `get_result` raise rather than
  silently no-op.
- **Works unchanged from `async def` tests** — same names, nothing to `await` on the
  fixture. Enqueue with `await my_task.aenqueue()`.
- **Multi-DB: declare the Absurd alias** in the test's
  [`databases`](https://docs.djangoproject.com/en/6.0/topics/testing/tools/#django.test.TransactionTestCase.databases),
  or committed state leaks into the next test.

## Move durable time

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

`freeze_time(instant=None)` pins durable time for the block (`None` = real now). Its
`FrozenTime` handle has the only two movers, `move_to(datetime)` and `shift(timedelta)`,
and each moves Python's clock (via
[time-machine](https://github.com/adamchainz/time-machine)) and Postgres's
`absurd.fake_now` together.

- **Enter the block before the `enqueue()` calls whose deadlines you want to control.**
  Freezing to a past instant after rows already exist leaves their deadlines in the
  database's future, so nothing is claimable until a later move passes them.
- `shift(Δ)` is absolute elapsed time, not wall-clock arithmetic — seven days across a
  spring-forward morning is 7 × 24 hours, which is what a durable deadline measures.
- Blocks don't nest, and a `FrozenTime` raises once its block has exited. Sequential
  blocks are fine.
- **Install [time-machine](https://github.com/adamchainz/time-machine) yourself** — it's
  a test dependency of _your_ project. Only `freeze_time` needs it, and it raises
  `ImproperlyConfigured` naming the install command if missing.
- **Don't enqueue across a savepoint rollback.** The rollback reverts Django's session
  clock, so a later `enqueue()` stamps real time and won't look claimable.
- **A freeze doesn't reach [pg_cron](cron-jobs.md#postgres-side-pg_cron)** — its
  launcher runs in another database on its own clock. See [below](#schedule-in-a-test).

`FrozenTime`, `AbsurdTestRuntime` (what `dj_absurd` is typed as), `TaskSnapshot`, and
`RunSnapshot` are importable from `django_absurd.test` for annotating your own helpers.

## Fixture API

### `dj_absurd.drain(queue="default")` { #drain data-toc-label="drain()" }

Runs every currently-claimable task on `queue` to completion, one at a time, returning
one `RunSnapshot` per run executed, in claim order.

| Field              | Meaning                                                                          |
| ------------------ | -------------------------------------------------------------------------------- |
| `queue`, `task_id` | which task this run belongs to                                                   |
| `run_id`           | this run's id — the same value appears twice for a re-armed `await_event` waiter |
| `task_name`        | dotted task path                                                                 |
| `args`, `kwargs`   | decoded from the enqueued params                                                 |
| `attempt`          | 1-based attempt number                                                           |
| `state`            | see the state vocabulary below                                                   |
| `result`           | the task's return value, once `completed`                                        |
| `failure`          | `{"message": str, "name"?: str, "traceback"?: str}`, once `failed`               |

| State       | Meaning                                                                                                                           |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `pending`   | claimable, not yet run                                                                                                            |
| `sleeping`  | suspended — a durable [sleep](workflows.md#sleep), an `await_event` wait, or a retry backoff (indistinguishable from a run alone) |
| `completed` | finished successfully                                                                                                             |
| `failed`    | raised, and out of retries                                                                                                        |
| `cancelled` | cancelled before or during execution                                                                                              |

- **`drain` provisions nothing**, unlike the CLI. `migrate` leaves a test database
  ready, but a queue declared by overriding `TASKS` in one test has no table — call
  `sync_queues()` first or `drain()` raises `QueueNotProvisionedError`. Undeclared
  raises `QueueNotDeclaredError`; see [exceptions](configuration.md#exceptions).

### `dj_absurd.emit(name, payload=None, queue="default")` { #emit data-toc-label="emit()" }

```python
dj_absurd.emit(f"warehouse.packed:{order_id}", {"tracking": "abc"})
```

Delivers an [event](workflows.md#events), resolving a task suspended in `await_event` —
the waiter resumes on the next `drain()`. An unprovisioned queue raises
`QueueNotProvisionedError`, same as `drain()`.

### `dj_absurd.get_result(task_id, queue=...)` { #get-result data-toc-label="get_result()" }

```python
result = reports_task.enqueue()  # id is "reports:<uuid>"
dj_absurd.get_result(result.id)  # queries the "reports" queue
```

Returns a `TaskSnapshot`, or raises `TaskNotFoundError`. Unlike
[`my_task.get_result()`](tasks.md#read-the-result) it reads Absurd's own states —
including `sleeping`, which `TaskResult.status` can't show.

| Field              | Meaning                                           |
| ------------------ | ------------------------------------------------- |
| `queue`, `task_id` | which task this is (no queue prefix on `task_id`) |
| `task_name`        | dotted task path                                  |
| `args`, `kwargs`   | decoded from the enqueued params                  |
| `state`            | see the state vocabulary under `drain()`          |
| `attempts`         | attempts CREATED, not completed (see below)       |
| `enqueued_at`      | when `enqueue()` ran                              |
| `result`           | the task's return value, once `completed`         |
| `failure`          | `None` except on a terminal failure (see below)   |

- `task_id` takes a bare uuid or a prefixed `"queue:uuid"`. The prefix wins over
  `queue`'s default; a `queue=` that disagrees raises `TaskIdQueueMismatchError`.
- **This view can't express an in-flight [retry](tasks.md#retries-spawn-options):**
  `attempts` reads `2` before the second attempt runs, `state="sleeping"` covers a
  backoff as well as a durable sleep, and `failure` is `None` mid-backoff. Use
  `drain()`'s `RunSnapshot` to tell them apart.
- **A deferred task's id names its wrapper.** A [`run_after`](tasks.md#run-it-later)
  enqueue creates a `<task path>:run_after` row and this reports that row. Use Django's
  own `get_result` for your task's status and return value.

### `dj_absurd.sync_queues()` { #sync-queues data-toc-label="sync_queues()" }

Provisions every declared queue — the runtime counterpart of
`manage.py absurd_sync_queues`. Only needed when a test changes queue topology, such as
a `settings` override declaring a queue the migration never saw.

### `dj_absurd.now` { #now data-toc-label="now" }

Virtual now, timezone-aware, as Postgres reports it — read over a fresh connection, not
computed in Python.

## Cleanup is automatic

pytest users do nothing — the plugin wires cleanup into Django's own test teardown. No
fixture to request, no marker to add.

- Plain `TestCase` / `db` tests need none: the `enqueue()` rides the same uncommitted
  transaction Django
  [rolls back](https://docs.djangoproject.com/en/6.0/topics/testing/overview/#rollback-emulation).
- `transaction=True` tests commit for real, so queue state is truncated after each — and
  with [`django_absurd.pg_cron`](cron-jobs.md#postgres-side-pg_cron) installed, its
  settings- and admin-authored jobs plus the
  [`OPTIONS["CLEANUP"]`](cleanup.md#schedule-recurring-cleanup) job are unscheduled too.
- Multi-DB: cleanup only runs when the test's `databases` includes the Absurd alias.
- No DB access means no Absurd access — `enqueue()` trips pytest-django's own
  [blocking](https://pytest-django.readthedocs.io/en/latest/database.html) like any
  query.

## Getting a `SCHEDULE` into pg_cron for a test { #schedule-in-a-test data-toc-label="SCHEDULE in a test" }

```python
settings.TASKS["default"]["OPTIONS"]["PG_CRON_ON_TEST_DB"] = True
call_command("absurd_sync_crons")
```

Every `cron.*` write is [inert on a test database](cron-jobs.md#test-databases) by
default, so a [`SCHEDULE`](cron-jobs.md#declare-a-schedule) can't fire for real against
test data. `PG_CRON_ON_TEST_DB` is the opt-in.

- Without it, `absurd_sync_crons` refuses to run rather than silently doing nothing.
- Using `migrate`'s automatic reconcile instead also needs
  `SYNC_SCHEDULES_ON_TEST_DB = True`.
- Cleanup clears whatever ends up in `cron.job` / `ScheduledTask` either way.

## `manage.py test` { #manage-py-test data-toc-label="manage.py test" }

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
same public hook yourself and point `TEST_RUNNER` at your subclass.
`install_absurd_cleanup()` is idempotent.
