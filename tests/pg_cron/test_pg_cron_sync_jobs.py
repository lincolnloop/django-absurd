import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, connections, transaction
from pytest_django.fixtures import SettingsWrapper

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import prune_pg_cron_jobs
from django_absurd.pg_cron.reconcile import sync_crons
from tests.pg_cron import utils

pytestmark = pytest.mark.django_db(transaction=True)


def live_database() -> str:
    return str(connections["default"].settings_dict["NAME"])


def test_creates_job_with_schedule_and_constant_command(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])

    live_db = live_database()
    rows = utils.fetch_managed_jobs(live_db)
    assert len(rows) == 1
    jobname, schedule, command, active = rows[0]
    assert jobname == catalog.build_jobname(live_db, Source.SETTINGS, "a")
    assert schedule == "0 2 * * *"
    assert command == "select public.django_absurd_run_scheduled('s', 'a')"
    assert active is True


def test_sync_is_idempotent(settings: SettingsWrapper) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    sync_crons(get_absurd_backends()["default"])

    live_db = live_database()
    rows = utils.fetch_managed_jobs(live_db)
    assert len(rows) == 1
    assert rows[0][0] == catalog.build_jobname(live_db, Source.SETTINGS, "a")


def test_prune_removes_undeclared_job_but_keeps_foreign(
    settings: SettingsWrapper,
) -> None:
    with connection.cursor() as cur:
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
    live_db = live_database()
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

    with connection.cursor() as cur:
        cur.execute("select count(*) from cron.job where jobname = 'keepme'")
        assert cur.fetchone()[0] == 1
        cur.execute("select cron.unschedule('keepme')")  # don't leak the foreign job


def test_prune_tolerates_already_unscheduled_job(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    sync_crons(get_absurd_backends()["default"])
    live_db = live_database()

    # Pre-remove job b's cron.job row out-of-band; prune must swallow the
    # "could not find valid entry" error and still complete.
    with connection.cursor() as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [catalog.build_jobname(live_db, Source.SETTINGS, "b")],
        )
        jobid = cur.fetchone()[0]
        cur.execute("select cron.unschedule(%s)", [jobid])

    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])  # no exception

    assert {r[0] for r in utils.fetch_managed_jobs(live_db)} == {
        catalog.build_jobname(live_db, Source.SETTINGS, "a")
    }


def test_prune_swallows_job_vanished_after_stale_scan(
    settings: SettingsWrapper,
) -> None:
    # The stale-id scan and the unschedule are separate steps; a concurrent actor
    # can remove a job's cron.job row in between. prune_pg_cron_jobs must swallow
    # the resulting "could not find" error and finish the reconcile.
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    live_db = live_database()

    with connection.cursor() as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [catalog.build_jobname(live_db, Source.SETTINGS, "a")],
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

    assert utils.fetch_managed_jobs(live_db) == []


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
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    live_db = live_database()

    with connection.cursor() as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [catalog.build_jobname(live_db, Source.SETTINGS, "a")],
        )
        jobid = cur.fetchone()[0]
        cur.execute("select cron.alter_job(%s, active := false)", [jobid])

    sync_crons(get_absurd_backends()["default"])

    rows = utils.fetch_managed_jobs(live_db)
    assert len(rows) == 1
    assert rows[0][3] is True


def test_injection_args_are_quoted_and_schema_survives(
    settings: SettingsWrapper,
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

    rows = utils.fetch_managed_jobs(live_database())
    assert len(rows) == 1
    assert rows[0][2] == "select public.django_absurd_run_scheduled('s', 'evil')"

    with connection.cursor() as cur:
        cur.execute("select to_regnamespace('absurd')")
        assert cur.fetchone()[0] is not None, "absurd schema was dropped by injection"
