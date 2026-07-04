import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, connections, transaction

from django_absurd.backends import get_absurd_backends
from django_absurd.pgcron import build_jobname, prune_pgcron_jobs, sync_crons

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.pgcron,
    pytest.mark.usefixtures("ensure_pgcron"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"


def tasks(schedule):
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


def owned_jobs(alias="default"):
    with connection.cursor() as cur:
        cur.execute(
            "select jobname, schedule, command, active from cron.job "
            "where jobname like %s order by jobname",
            [f"absurd:settings:{alias}:%"],
        )
        return cur.fetchall()


@pytest.fixture(autouse=True)
def _clear_owned_jobs():
    yield
    with connection.cursor() as cur:
        cur.execute("select jobid from cron.job where jobname like 'absurd:%'")
        for (jobid,) in cur.fetchall():
            cur.execute("select cron.unschedule(%s)", [jobid])


def test_creates_job_with_schedule_and_constant_command(settings):
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    sync_crons(get_absurd_backends()["default"])

    rows = owned_jobs()
    assert len(rows) == 1
    jobname, schedule, command, active = rows[0]
    assert jobname == "absurd:settings:default:a"
    assert schedule == "0 2 * * *"
    assert (
        command
        == "select public.django_absurd_run_scheduled('settings', 'default', 'a')"
    )
    assert active is True


def test_sync_is_idempotent(settings):
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    sync_crons(get_absurd_backends()["default"])
    sync_crons(get_absurd_backends()["default"])

    rows = owned_jobs()
    assert len(rows) == 1
    assert rows[0][0] == "absurd:settings:default:a"


def test_prune_removes_undeclared_job_but_keeps_foreign(settings):
    with connection.cursor() as cur:
        cur.execute(
            "select cron.schedule(%s, %s, %s)", ["keepme", "* * * * *", "select 1"]
        )

    settings.TASKS = tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    sync_crons(get_absurd_backends()["default"])
    assert {r[0] for r in owned_jobs()} == {
        "absurd:settings:default:a",
        "absurd:settings:default:b",
    }

    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    sync_crons(get_absurd_backends()["default"])
    assert {r[0] for r in owned_jobs()} == {"absurd:settings:default:a"}

    with connection.cursor() as cur:
        cur.execute("select count(*) from cron.job where jobname = 'keepme'")
        assert cur.fetchone()[0] == 1


def test_prune_tolerates_already_unscheduled_job(settings):
    settings.TASKS = tasks(
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

    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    sync_crons(get_absurd_backends()["default"])  # no exception

    assert {r[0] for r in owned_jobs()} == {"absurd:settings:default:a"}


def test_prune_swallows_job_vanished_after_stale_scan(settings):
    # The stale-id scan and the unschedule are separate steps; a concurrent actor
    # can remove a job's cron.job row in between. prune_pgcron_jobs must swallow
    # the resulting "could not find" error and finish the reconcile.
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
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
        prune_pgcron_jobs(cur, [jobid])  # dangling id -> swallowed, no exception

    assert owned_jobs() == []


def test_prune_reraises_unexpected_error(settings):
    # A non-"could not find" DatabaseError (bad cast) is not swallowed.
    with (
        transaction.atomic(),
        connection.cursor() as cur,
        pytest.raises(DatabaseError),
    ):
        prune_pgcron_jobs(cur, [{"bad": "type"}])


def test_rearm_reenables_disabled_job(settings):
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    sync_crons(get_absurd_backends()["default"])

    with connection.cursor() as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [build_jobname("default", "a")],
        )
        jobid = cur.fetchone()[0]
        cur.execute("select cron.alter_job(%s, active := false)", [jobid])

    sync_crons(get_absurd_backends()["default"])

    rows = owned_jobs()
    assert len(rows) == 1
    assert rows[0][3] is True


def test_injection_args_are_quoted_and_schema_survives(settings):
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute("select to_regnamespace('absurd')")
        assert cur.fetchone()[0] is not None

    settings.TASKS = tasks(
        {
            "evil": {
                "task": "tests.tasks.add",
                "cron": "* * * * *",
                "args": ["'; drop schema absurd cascade; --", "$$"],
            }
        }
    )
    sync_crons(get_absurd_backends()["default"])

    rows = owned_jobs()
    assert len(rows) == 1
    assert (
        rows[0][2]
        == "select public.django_absurd_run_scheduled('settings', 'default', 'evil')"
    )

    with connection.cursor() as cur:
        cur.execute("select to_regnamespace('absurd')")
        assert cur.fetchone()[0] is not None, "absurd schema was dropped by injection"
