---
icon: lucide/flask-conical
---

# Testing

django-absurd ships a [pytest](https://docs.pytest.org/) plugin — installing the package
registers it automatically via a
[`pytest11` entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others),
no extra setup required. It builds on
[pytest-django](https://pytest-django.readthedocs.io/), so install that alongside
django-absurd (`pip install pytest-django`).

## The `dj_absurd` fixture

Running a task, moving Absurd's own notion of "now", and inspecting what actually ran
all go through one fixture — whether the test is a one-line "my task completes" or a
[durable sleep](workflows.md#sleep), an [`await_event` timeout](workflows.md#timeout), a
retry backoff, or a chain of several sleeps.

```python
import datetime as dt

import pytest

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_task_sleeps_seven_days_then_completes(dj_absurd):
    with dj_absurd.freeze_time(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) as frozen_time:
        result = my_weekly_followup_task.enqueue()  # enqueue INSIDE the block
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=7))
        assert [run.state for run in dj_absurd.drain()] == ["completed"]

        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.state == "completed"
```

### Requires `transaction=True`

Absurd's own work runs on a connection separate from the test's; under a plain
[`db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#db) test the enqueued
row is invisible to it. `drain`, `emit`, and `get_result` detect the open transaction
and raise rather than silently no-opping — use
`@pytest.mark.django_db(transaction=True)`.

!!! warning "Multi-DB: declare the Absurd alias"

    Draining commits real state via the worker's own connection. If that test's
    `databases` attribute doesn't include the Absurd alias, cleanup skips it afterward
    and the committed state leaks into the next test. Declare the Absurd alias in
    `databases` on any `transaction=True` test that runs a worker.

### `freeze_time(instant=None)`

Pins durable time for the block, `None` meaning real now at entry. It yields a
`FrozenTime` handle whose two movers are the only way durable time ever moves:
`move_to(datetime)` for an absolute, timezone-aware instant, and `shift(timedelta)` to
move forward by Δ. Both move Python's clock (via
[time-machine](https://github.com/adamchainz/time-machine)) and Postgres's
`absurd.fake_now` GUC together.

**Enter the block before the `enqueue()` calls whose deadlines you want to control.**
Freezing to a PAST instant after rows already exist leaves those rows' deadlines in the
database's future relative to the new frozen now, so nothing is claimable until a later
`move_to`/`shift` passes them.

`shift(Δ)` is absolute elapsed time, not wall-clock arithmetic: shifting seven days from
`01:30` the morning of a US spring-forward lands 7 × 24 hours later as an instant, which
is the only thing a durable deadline is measured in.

Both halves of the clock are released when the block ends, so a test can open several
windows in sequence. Opening one INSIDE another raises instead of stacking — two frozen
instants cannot both be "now" — and using a `FrozenTime` after its own block exited
raises rather than silently re-freezing from real now. A test that never opens a block
pays nothing: the other members never touch the clock. `FrozenTime` is importable from
`django_absurd.test` for annotating your own helpers.

!!! warning "Install time-machine yourself"

    [time-machine](https://github.com/adamchainz/time-machine) is a dev/test
    dependency of *your* project, not bundled with django-absurd and not one of its
    extras. `pip install time-machine` in your test environment. `sync_queues`/`drain`/
    `emit`/`get_result`/`now` work without it — only `freeze_time` imports it, lazily,
    on first use, and raises `ImproperlyConfigured` naming the install command if it's
    missing.

### `sync_queues()`

Provisions every declared queue — the runtime counterpart of
`manage.py absurd_sync_queues`. Rarely needed: `migrate` already provisions the declared
catalog, so reach for this only when the test itself changed queue topology — a
`settings` override declaring a queue the migration never saw, or a fixture that dropped
the queues.

### `drain(queue="default")`

Runs every currently-claimable task on `queue` to completion, in-process — no
[worker](how-it-works.md#workers) subprocess, no polling loop to manage. It's the
fixture counterpart of `absurd_worker --burst`: it drains the backlog present at call
time, then returns one `RunSnapshot` per run executed, in claim order.

It provisions nothing, unlike the CLI, which provisions declared queues on start.
`migrate` provisions every declared queue already, so a test database arrives ready. A
queue a single test declares by overriding `TASKS` has no table yet — call
`dj_absurd.sync_queues()` first, or `drain()` raises `QueueNotProvisionedError` naming
the `absurd_sync_queues` command. Draining a queue that isn't declared at all raises
`QueueNotDeclaredError` — see [Our own exceptions](workflows.md#our-own-exceptions).

### `emit(name, payload=None, queue="default")`

Delivers an [event](workflows.md#events), resolving a task suspended in `await_event` —
the waiter resumes on the next `drain()`. An unprovisioned queue raises
`QueueNotProvisionedError`, same as `drain()`.

### `get_result(task_id, queue=...)`

Looks up one task, returning `TaskSnapshot | None`. `task_id` accepts either a bare uuid
or Django's own `TaskResult.id` (`"queue:uuid"`) — whatever `enqueue()` handed back.
When it carries a queue prefix, that prefix is what gets queried, not `queue`'s default.
Omit `queue` entirely to let the prefix win:

```python
result = reports_task.enqueue()   # id is "reports:<uuid>"
dj_absurd.get_result(result.id)   # queries the "reports" queue
```

An explicit `queue=` — even `queue="default"` — that disagrees with a prefixed id's own
queue raises `TaskIdQueueMismatchError` naming both. A bare uuid resolves an unpassed
`queue` to `"default"`.

A task-level view cannot express an in-flight [retry](tasks.md#retries-spawn-options):

- **`attempts` counts attempts CREATED, not completed.** A task with one failed attempt
  and a pending backoff already reads `attempts=2`, before the second attempt has run.
- **`state="sleeping"` covers a retry backoff as well as a durable
  [sleep](workflows.md#sleep).** A test asserting "my workflow is asleep" would pass
  just as readily on a task that crashed and is waiting to retry.
- **`failure` is `None` mid-backoff.** `last_attempt_run` already points at the fresh
  pending run by the time the backoff is showing, so the failed attempt's reason isn't
  reachable from this snapshot.

`drain()`'s `RunSnapshot` is how you tell these apart — it reports each run's own state
right after that run executes, so a retry sequence reads attempt-by-attempt instead of
collapsing to one ambiguous final read.

### `now`

Virtual now, timezone-aware, as Postgres itself reports it — read through the fixture's
own fresh connection rather than computed in Python.

### Hazards

**A `manage.py absurd_worker` subprocess is only half-frozen.** `ALTER DATABASE` reaches
its Postgres session, but its own Python clock is real — frozen-ahead-of-real is the
deadlock direction, since a durable sleep due at the frozen instant looks not-yet-due to
that process's real clock. `drain` only ever runs the in-process burst worker; a real
subprocess worker under a freeze is out of scope.

**A savepoint rollback inside a `freeze_time` block can make a later `enqueue()` stamp
stale time.** Django's own connection only ever sees the frozen instant via a
session-level `SET` (a database-level default reaches only new sessions, not one Django
already has open), and a savepoint rollback reverts that `SET`. `dj_absurd.now` still
reports the frozen instant correctly — it reads through its own fresh connection, which
sees the database-level default — so `now` cannot itself flag the mismatch. If your test
rolls back a savepoint inside the block, avoid enqueuing across the rollback boundary.

**Advancing cannot make a [pg_cron](cron-jobs.md#database-side-pg_cron) schedule fire.**
Its launcher runs in the central `cron.database_name` database
([Test databases](cron-jobs.md#test-databases)), on its own real clock, and interprets
schedules in the [`cron.timezone`](cron-jobs.md#timezone) GUC — none of which a
test-database GUC can reach. Testing a pg_cron schedule stays "reconcile it in, then
inspect `cron.job`" — see
[Getting a `SCHEDULE` into pg_cron for a test](#getting-a-schedule-into-pg_cron-for-a-test).

## Cleanup is automatic

pytest users do nothing: the plugin wires Absurd state cleanup into Django's own test
teardown — exact parity with how Django resets its own tables. There's no fixture to
request and no marker to add.

- Plain
  `TestCase`/[`db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#db)
  tests are cleaned by Django's own
  [rollback](https://docs.djangoproject.com/en/6.0/topics/testing/overview/#rollback-emulation)
  — an `enqueue()` rides the same uncommitted transaction, so there's nothing left to
  flush once the test ends.
- `transaction=True`/[`transactional_db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#transactional-db)
  tests (real
  [`TransactionTestCase`](https://docs.djangoproject.com/en/6.0/topics/testing/tools/#django.test.TransactionTestCase)s)
  commit for real, so django-absurd truncates queue state after each one — and, when
  [`django_absurd.pg_cron`](cron-jobs.md#database-side-pg_cron) is installed,
  unschedules its own settings- and admin-authored jobs, plus the cleanup job if
  [`OPTIONS["CLEANUP"]`](cleanup.md#schedule-recurring-cleanup) is set — right alongside
  Django's own post-test flush.

## No database access, no Absurd access

A test with no DB access can't touch Absurd either: `enqueue()` goes through Django's
database connection, so it trips pytest-django's own
[database access blocking](https://pytest-django.readthedocs.io/en/latest/database.html)
the same as any other query — the same `RuntimeError` telling you to request
`django_db`/`db`/`transactional_db`.

In a multi-DB project, cleanup only runs for a test whose declared
[`databases`](https://docs.djangoproject.com/en/6.0/topics/testing/tools/#django.test.TransactionTestCase.databases)
attribute includes the Absurd alias (respecting the `"__all__"` sentinel) — an
undeclared alias is skipped, matching Django's own per-alias flush scoping.

## Getting a `SCHEDULE` into pg_cron for a test

Auto-cleanup only tears down; it has no say over whether a
[`SCHEDULE`](cron-jobs.md#declare-a-schedule) entry lands in `pg_cron` in the first
place. By default every `cron.*` write for a backend is
[inert on a test database or during an active test run](cron-jobs.md#test-databases) —
detected automatically, no settings changes needed — precisely so a `SCHEDULE` doesn't
start firing for real against test data.

A test that genuinely needs a real job in [pg_cron](cron-jobs.md#database-side-pg_cron)
needs `OPTIONS["PG_CRON_ON_TEST_DB"] = True` for that backend first — that's the opt-in
out of inertness. With it set, either let `migrate`'s automatic reconcile run (also
requires `OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"] = True`) or call
`call_command("absurd_sync_crons")` explicitly — without `PG_CRON_ON_TEST_DB`,
`absurd_sync_crons` refuses to run (`CommandError`) rather than silently doing nothing.
Either way, cleanup clears whatever ended up in `cron.job` / `ScheduledTask` —
settings-synced, admin-authored, or created directly by the test itself.

## `manage.py test`

Django's own
[`DiscoverRunner`](https://docs.djangoproject.com/en/6.0/topics/testing/advanced/#django.test.runner.DiscoverRunner)
has no equivalent auto-hook — pytest is django-absurd's primary test surface. Wire the
same public hook yourself, from a runner subclass, and point `TEST_RUNNER` at it:

```python
from django.test.runner import DiscoverRunner

from django_absurd.test import install_absurd_cleanup


class MyTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        install_absurd_cleanup()
```

`install_absurd_cleanup()` is idempotent — calling it where pytest's plugin already
installed it is a no-op.
