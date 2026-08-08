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


def test_my_task_completes(dj_absurd):
    result = my_task.enqueue()

    assert [run.state for run in dj_absurd.drain()] == ["completed"]
    assert dj_absurd.get_result(result.id).state == "completed"
```

`dj_absurd` is the only fixture. `drain()` runs every claimable task to completion
in-process — no [worker](how-it-works.md#workers) subprocess, no polling loop to manage.

- **`transaction=True` is required.** Absurd works on a connection separate from the
  test's, so under a plain
  [`db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#db) test the
  enqueued row is invisible to it. `drain`, `emit`, and `get_result` detect the open
  transaction and raise rather than silently no-opping.
- **Every member works unchanged from an `async def` test** — same names, nothing to
  `await` on the fixture. Enqueue with Django's own `await my_task.aenqueue()`, since
  `enqueue()` is synchronous.
- **Multi-DB: declare the Absurd alias** in the test's
  [`databases`](https://docs.djangoproject.com/en/6.0/topics/testing/tools/#django.test.TransactionTestCase.databases).
  Draining commits real state through the worker's own connection; without the alias
  declared, cleanup skips it afterwards and that state leaks into the next test.

## Move durable time

```python
import datetime as dt


def test_a_task_sleeps_seven_days_then_completes(dj_absurd):
    with dj_absurd.freeze_time(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) as frozen_time:
        result = my_weekly_followup_task.enqueue()   # enqueue INSIDE the block
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=7))
        assert [run.state for run in dj_absurd.drain()] == ["completed"]
        assert dj_absurd.get_result(result.id).state == "completed"
```

`freeze_time(instant=None)` pins durable time for the block — `None` means real now at
entry — and yields a `FrozenTime` whose two movers are the only way durable time ever
moves: `move_to(datetime)` and `shift(timedelta)`. Both move Python's clock (via
[time-machine](https://github.com/adamchainz/time-machine)) and Postgres's
`absurd.fake_now` GUC together.

- **Enter the block before the `enqueue()` calls whose deadlines you want to control.**
  Freezing to a past instant after rows already exist leaves their deadlines in the
  database's future, so nothing is claimable until a later move passes them.
- `shift(Δ)` is absolute elapsed time, not wall-clock arithmetic. Seven days from
  `01:30` on a spring-forward morning lands 7 × 24 hours later as an instant, which is
  the only thing a durable deadline measures.
- Blocks don't nest — two frozen instants can't both be "now", so opening one inside
  another raises, as does using a `FrozenTime` after its own block exited. Sequential
  blocks are fine.
- **Install [time-machine](https://github.com/adamchainz/time-machine) yourself.** It's
  a test dependency of _your_ project, not bundled with django-absurd and not one of its
  extras. Only `freeze_time` imports it, lazily on first use, raising
  `ImproperlyConfigured` naming the install command if it's missing.
- **A savepoint rollback inside the block reverts Django's session clock**, so a later
  `enqueue()` stamps real time and won't look claimable. Don't enqueue across a rollback
  boundary.
- **A freeze doesn't reach [pg_cron](cron-jobs.md#postgres-side-pg_cron).** Its launcher
  runs in another database on its own clock, so advancing durable time cannot make a
  schedule fire — see [below](#getting-a-schedule-into-pg_cron-for-a-test).

A test that never opens a block pays nothing; the other members never touch the clock.
`FrozenTime`, `AbsurdTestRuntime` (what `dj_absurd` is typed as), `TaskSnapshot`, and
`RunSnapshot` are all importable from `django_absurd.test` for annotating your own
helpers.

## Fixture API

### `drain(queue="default")`

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

- **`drain` provisions nothing**, unlike the CLI. `migrate` already provisions the
  declared catalog, so a test database arrives ready — but a queue a single test
  declares by overriding `TASKS` has no table yet. Call `sync_queues()` first, or
  `drain()` raises `QueueNotProvisionedError`. An undeclared queue raises
  `QueueNotDeclaredError`; see [exceptions](configuration.md#exceptions).

### `emit(name, payload=None, queue="default")`

Delivers an [event](workflows.md#events), resolving a task suspended in `await_event` —
the waiter resumes on the next `drain()`. An unprovisioned queue raises
`QueueNotProvisionedError`, same as `drain()`.

### `get_result(task_id, queue=...)`

```python
result = reports_task.enqueue()   # id is "reports:<uuid>"
dj_absurd.get_result(result.id)   # queries the "reports" queue
```

Looks up one task and returns a `TaskSnapshot`, raising `TaskNotFoundError` on a miss.
Where [`my_task.get_result(result.id)`](tasks.md#read-the-result) reads Django's
`TaskResult.status`, this reads Absurd's own states directly — including `sleeping`,
which `TaskResult.status` can't show — and skips the worker round-trip.

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

- `task_id` accepts a bare uuid or Django's `TaskResult.id` (`"queue:uuid"`). A prefix
  wins over `queue`'s default; an explicit `queue=` that disagrees with the prefix
  raises `TaskIdQueueMismatchError`. A bare uuid with no `queue` resolves to
  `"default"`.
- **A task-level view cannot express an in-flight
  [retry](tasks.md#retries-spawn-options).** `attempts` already reads `2` before the
  second attempt has run; `state="sleeping"` covers a retry backoff as well as a durable
  sleep; and `failure` is `None` mid-backoff, because `last_attempt_run` already points
  at the fresh pending run. Use `drain()`'s `RunSnapshot` to tell these apart — it
  reports each run's own state right after that run executes.
- **A deferred task's id names its wrapper.** A [`run_after`](tasks.md#run-it-later)
  enqueue creates a `<your task path>:run_after` row, and this method reports the row
  the id names — the fixture is for inspecting state that really exists. Use Django's
  own `get_result` when you want your task's status and return value.

### `sync_queues()`

Provisions every declared queue — the runtime counterpart of
`manage.py absurd_sync_queues`. Rarely needed: reach for it only when the test itself
changed queue topology, such as a `settings` override declaring a queue the migration
never saw.

### `now`

Virtual now, timezone-aware, as Postgres itself reports it — read through the fixture's
own fresh connection rather than computed in Python.

## Cleanup is automatic

pytest users do nothing. The plugin wires Absurd state cleanup into Django's own test
teardown — no fixture to request, no marker to add.

- Plain `TestCase` / `db` tests are cleaned by Django's own
  [rollback](https://docs.djangoproject.com/en/6.0/topics/testing/overview/#rollback-emulation).
  An `enqueue()` rides the same uncommitted transaction, so nothing is left to flush.
- `transaction=True` tests commit for real, so django-absurd truncates queue state after
  each one — and, with [`django_absurd.pg_cron`](cron-jobs.md#postgres-side-pg_cron)
  installed, unschedules its own settings- and admin-authored jobs plus the
  [`OPTIONS["CLEANUP"]`](cleanup.md#schedule-recurring-cleanup) job.
- In a multi-DB project cleanup only runs for a test whose `databases` includes the
  Absurd alias (respecting `"__all__"`), matching Django's own per-alias flush scoping.
- A test with no DB access can't touch Absurd either: `enqueue()` goes through Django's
  connection, so it trips pytest-django's own
  [database access blocking](https://pytest-django.readthedocs.io/en/latest/database.html)
  like any other query.

## Getting a `SCHEDULE` into pg_cron for a test

```python
settings.TASKS["default"]["OPTIONS"]["PG_CRON_ON_TEST_DB"] = True
call_command("absurd_sync_crons")
```

Every `cron.*` write is [inert on a test database](cron-jobs.md#test-databases) by
default — detected automatically — precisely so a
[`SCHEDULE`](cron-jobs.md#declare-a-schedule) doesn't start firing for real against test
data. `PG_CRON_ON_TEST_DB` is the opt-in.

- Without it, `absurd_sync_crons` refuses to run (`CommandError`) rather than silently
  doing nothing.
- Letting `migrate`'s automatic reconcile do the work instead also requires
  `SYNC_SCHEDULES_ON_TEST_DB = True`.
- Either way, cleanup clears whatever ended up in `cron.job` / `ScheduledTask` —
  settings-synced, admin-authored, or created directly by the test.

## `manage.py test`

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
