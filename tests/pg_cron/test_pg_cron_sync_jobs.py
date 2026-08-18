import pytest
from django.core.management import call_command
from django.db import connection
from pytest_django import Settings

from django_absurd.backends import get_absurd_backends
from django_absurd.connection import open_central_connection
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.reconcile import sync_crons
from tests.pg_cron import utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_creates_job_with_schedule_and_constant_command(
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])

    live_db = utils.fetch_live_database()
    rows = utils.fetch_managed_jobs(live_db)
    assert len(rows) == 1
    jobname, schedule, command, active = rows[0]
    assert jobname == catalog.build_jobname(live_db, Source.SETTINGS, "a")
    assert schedule == "0 2 * * *"
    assert command == "select public.django_absurd_run_scheduled('s', 'a')"
    assert active is True


def test_sync_is_idempotent(settings: Settings) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    sync_crons(get_absurd_backends()["default"])

    live_db = utils.fetch_live_database()
    rows = utils.fetch_managed_jobs(live_db)
    assert len(rows) == 1
    assert rows[0][0] == catalog.build_jobname(live_db, Source.SETTINGS, "a")


def test_prune_removes_undeclared_job_but_keeps_foreign(
    settings: Settings,
) -> None:
    with open_central_connection("default") as cur:
        cur.execute(
            "select cron.schedule(%s, %s, %s)", ["keepme", "* * * * *", "select 1"]
        )

    settings.TASKS = utils.build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    sync_crons(get_absurd_backends()["default"])
    live_db = utils.fetch_live_database()
    assert {r[0] for r in utils.fetch_managed_jobs(live_db)} == {
        catalog.build_jobname(live_db, Source.SETTINGS, "a"),
        catalog.build_jobname(live_db, Source.SETTINGS, "b"),
    }

    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    assert {r[0] for r in utils.fetch_managed_jobs(live_db)} == {
        catalog.build_jobname(live_db, Source.SETTINGS, "a")
    }

    with open_central_connection("default") as cur:
        cur.execute("select count(*) from cron.job where jobname = 'keepme'")
        assert cur.fetchone() == (1,)
        cur.execute("select cron.unschedule('keepme')")  # don't leak the foreign job


def test_prune_tolerates_already_unscheduled_job(
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    sync_crons(get_absurd_backends()["default"])
    live_db = utils.fetch_live_database()

    # Pre-remove job b's cron.job row out-of-band; prune must swallow the
    # "could not find valid entry" error and still complete.
    with open_central_connection("default") as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [catalog.build_jobname(live_db, Source.SETTINGS, "b")],
        )
        row = cur.fetchone()
        assert row is not None
        jobid = row[0]
        cur.execute("select cron.unschedule(%s)", [jobid])

    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])  # no exception

    assert {r[0] for r in utils.fetch_managed_jobs(live_db)} == {
        catalog.build_jobname(live_db, Source.SETTINGS, "a")
    }


def test_rearm_reenables_disabled_job(settings: Settings) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    live_db = utils.fetch_live_database()

    with open_central_connection("default") as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [catalog.build_jobname(live_db, Source.SETTINGS, "a")],
        )
        row = cur.fetchone()
        assert row is not None
        jobid = row[0]
        cur.execute("select cron.alter_job(%s, active := false)", [jobid])

    sync_crons(get_absurd_backends()["default"])

    rows = utils.fetch_managed_jobs(live_db)
    assert len(rows) == 1
    assert rows[0][3] is True


def test_injection_args_are_quoted_and_schema_survives(
    settings: Settings,
) -> None:
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute("select to_regnamespace('absurd')")
        assert cur.fetchone()[0] is not None

    settings.TASKS = utils.build_pg_cron_tasks(
        {
            "evil": {
                "task": "tests.tasks.add",
                "cron": "* * * * *",
                "args": ["'; drop schema absurd cascade; --", "$$"],
            }
        }
    )
    sync_crons(get_absurd_backends()["default"])

    rows = utils.fetch_managed_jobs(utils.fetch_live_database())
    assert len(rows) == 1
    assert rows[0][2] == "select public.django_absurd_run_scheduled('s', 'evil')"

    with connection.cursor() as cur:
        cur.execute("select to_regnamespace('absurd')")
        assert cur.fetchone()[0] is not None, "absurd schema was dropped by injection"
