---
icon: lucide/flask-conical
---

# Testing

django-absurd ships a [pytest](https://docs.pytest.org/) plugin — installing the package
registers it automatically via a
[`pytest11` entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others),
no extra setup required.

The plugin builds on [pytest-django](https://pytest-django.readthedocs.io/) — install it
in your test environment (`pip install pytest-django`) alongside django-absurd.

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

## `manage.py test` (non-pytest)

The pytest wiring above doesn't cover Django's own `manage.py test`/
[`DiscoverRunner`](https://docs.djangoproject.com/en/6.0/topics/testing/advanced/#django.test.runner.DiscoverRunner)
— not auto-wired in this release
([issue #96](https://github.com/lincolnloop/django-absurd/issues/96)). Call the same
hook yourself, from a runner subclass:

```python
from django.test.runner import DiscoverRunner

from django_absurd.test import install_absurd_cleanup


class MyTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        install_absurd_cleanup()
```

Then point your project's `TEST_RUNNER` at it. `install_absurd_cleanup()` is idempotent
— calling it again where pytest's plugin already installed it is a no-op.

## `absurd_drain_queue`

```python
@pytest.mark.django_db(transaction=True)
def test_task_runs(absurd_drain_queue):
    my_task.enqueue()
    absurd_drain_queue()
    ...
```

Returns a callable `drain(queue: str = "default", *, concurrency: int = 1) -> None` that
runs every currently-claimable task on `queue` to completion, in-process — no
[worker](how-it-works.md#workers) subprocess, no polling loop to manage. It's the
fixture equivalent of `absurd_worker --burst`: it drains the backlog present at call
time, then returns.

The worker opens its own database connection, separate from the test's — so a test using
`absurd_drain_queue` needs `transaction=True` (not plain `db`): under an uncommitted
`db` transaction the enqueue is invisible to that second connection and the task never
gets claimed.

!!! warning "Multi-DB: declare the Absurd alias"

    Draining commits real state via the worker's own connection. If that test's
    `databases` attribute doesn't include the Absurd alias, the cleanup guard above
    skips it afterward — the committed state leaks into the next test. Declare the
    Absurd alias in `databases` on any `transaction=True` test that runs a worker.

## Getting a `SCHEDULE` into pg_cron for a test

Auto-cleanup only tears down; it has no say over whether a
[`SCHEDULE`](cron-jobs.md#declare-a-schedule) entry lands in `pg_cron` in the first
place. By default, `migrate`'s automatic reconcile is
[skipped on a test database](cron-jobs.md#test-databases) — `SYNC_SCHEDULES_ON_TEST_DB`
defaults to `False`, precisely so a `SCHEDULE` doesn't start firing for real against
test data. A test that genuinely needs a real job in
[pg_cron](cron-jobs.md#database-side-pg_cron) either sets
`OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"] = True` or calls
`call_command("absurd_sync_crons")` explicitly. Either way, cleanup clears whatever
ended up in `cron.job` / `ScheduledTask` — settings-synced, admin-authored, or created
directly by the test itself — it clears whatever's present, regardless of how it got
there.
