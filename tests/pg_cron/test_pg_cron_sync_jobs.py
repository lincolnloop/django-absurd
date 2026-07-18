import typing as t

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, connections, transaction
from pytest_django.fixtures import SettingsWrapper

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask, prune_pg_cron_jobs
from django_absurd.pg_cron.reconcile import sync_crons
from django_absurd.pg_cron.validators import build_jobname

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks(
    schedule: t.Any,
) -> dict[str, dict[str, str | dict[str, t.Any]]]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "pg_cron",
                "SCHEDULE": schedule,
            },
        }
    }


def test_creates_job_with_schedule_and_constant_command(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])

    rows = ScheduledTask.pg_cron.get_managed_jobs()
    assert len(rows) == 1
    jobname, schedule, command, active = rows[0]
    assert jobname == "absurd:s:default:a"
    assert schedule == "0 2 * * *"
    assert command == "select public.django_absurd_run_scheduled('s', 'default', 'a')"
    assert active is True


def test_sync_is_idempotent(settings: SettingsWrapper) -> None:
    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    sync_crons(get_absurd_backends()["default"])

    rows = ScheduledTask.pg_cron.get_managed_jobs()
    assert len(rows) == 1
    assert rows[0][0] == "absurd:s:default:a"


def test_prune_removes_undeclared_job_but_keeps_foreign(
    settings: SettingsWrapper,
) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "select cron.schedule(%s, %s, %s)", ["keepme", "* * * * *", "select 1"]
        )

    settings.TASKS = build_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    sync_crons(get_absurd_backends()["default"])
    assert {r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()} == {
        "absurd:s:default:a",
        "absurd:s:default:b",
    }

    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    assert {r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()} == {
        "absurd:s:default:a"
    }

    with connection.cursor() as cur:
        cur.execute("select count(*) from cron.job where jobname = 'keepme'")
        assert cur.fetchone()[0] == 1
        cur.execute("select cron.unschedule('keepme')")  # don't leak the foreign job


def test_prune_tolerates_already_unscheduled_job(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    sync_crons(get_absurd_backends()["default"])

    # Pre-remove job b's cron.job row out-of-band; prune must swallow the
    # "could not find valid entry" error and still complete.
    with connection.cursor() as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [build_jobname("default", "b")],
        )
        jobid = cur.fetchone()[0]
        cur.execute("select cron.unschedule(%s)", [jobid])

    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])  # no exception

    assert {r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()} == {
        "absurd:s:default:a"
    }


def test_prune_swallows_job_vanished_after_stale_scan(
    settings: SettingsWrapper,
) -> None:
    # The stale-id scan and the unschedule are separate steps; a concurrent actor
    # can remove a job's cron.job row in between. prune_pg_cron_jobs must swallow
    # the resulting "could not find" error and finish the reconcile.
    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])

    with connection.cursor() as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [build_jobname("default", "a")],
        )
        jobid = cur.fetchone()[0]

    # Concurrent actor on a separate connection removes the row after the scan.
    params = connections["default"].get_connection_params()
    other = psycopg.connect(**params, autocommit=True)
    try:
        with other.cursor() as ocur:
            ocur.execute("select cron.unschedule(%s)", [jobid])
    finally:
        other.close()

    with transaction.atomic(), connection.cursor() as cur:
        prune_pg_cron_jobs(cur, [jobid])  # dangling id -> swallowed, no exception

    assert ScheduledTask.pg_cron.get_managed_jobs() == []


def test_prune_reraises_unexpected_error(
    settings: SettingsWrapper,
) -> None:
    # A non-"could not find" DatabaseError (bad cast) is not swallowed.
    with (
        transaction.atomic(),
        connection.cursor() as cur,
        pytest.raises(DatabaseError),
    ):
        prune_pg_cron_jobs(
            cur,
            [{"bad": "type"}],  # type: ignore[list-item]
        )


def test_rearm_reenables_disabled_job(settings: SettingsWrapper) -> None:
    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])

    with connection.cursor() as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [build_jobname("default", "a")],
        )
        jobid = cur.fetchone()[0]
        cur.execute("select cron.alter_job(%s, active := false)", [jobid])

    sync_crons(get_absurd_backends()["default"])

    rows = ScheduledTask.pg_cron.get_managed_jobs()
    assert len(rows) == 1
    assert rows[0][3] is True


def test_injection_args_are_quoted_and_schema_survives(
    settings: SettingsWrapper,
) -> None:
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute("select to_regnamespace('absurd')")
        assert cur.fetchone()[0] is not None

    settings.TASKS = build_tasks(
        {
            "evil": {
                "task": "tests.tasks.add",
                "cron": "* * * * *",
                "args": ["'; drop schema absurd cascade; --", "$$"],
            }
        }
    )
    sync_crons(get_absurd_backends()["default"])

    rows = ScheduledTask.pg_cron.get_managed_jobs()
    assert len(rows) == 1
    assert (
        rows[0][2]
        == "select public.django_absurd_run_scheduled('s', 'default', 'evil')"
    )

    with connection.cursor() as cur:
        cur.execute("select to_regnamespace('absurd')")
        assert cur.fetchone()[0] is not None, "absurd schema was dropped by injection"
