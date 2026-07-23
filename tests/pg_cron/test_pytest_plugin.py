import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.flush import flush_absurd_state
from django_absurd.pg_cron.models import ScheduledTask

pytestmark = pytest.mark.django_db(transaction=True)


def test_flush_absurd_state_drop_schema_true_unschedules_everything_blanket() -> None:
    scheduled_task = ScheduledTask.objects.create(
        source="a", name="direct", task="tests.tasks.add", cron="0 2 * * *"
    )
    assert scheduled_task.get_pg_cron_job() is not None
    with connection.cursor() as cur:
        cur.execute(
            "select cron.schedule(%s, %s, %s)",
            ["unrelated_job", "0 3 * * *", "select 1"],
        )

    flush_absurd_state(drop_schema=True)

    assert not ScheduledTask.objects.filter(name="direct", source="a").exists()
    with connection.cursor() as cur:
        cur.execute("select jobname from cron.job")
        assert cur.fetchall() == []  # blanket: even the unrelated job is gone


def test_flush_absurd_state_drop_schema_false_scopes_to_owned_jobs_only() -> None:
    scheduled_task = ScheduledTask.objects.create(
        source="a", name="direct", task="tests.tasks.add", cron="0 2 * * *"
    )
    with connection.cursor() as cur:
        cur.execute(
            "select cron.schedule(%s, %s, %s)",
            ["unrelated_job", "0 3 * * *", "select 1"],
        )

    flush_absurd_state()

    assert not ScheduledTask.objects.filter(name="direct", source="a").exists()
    assert scheduled_task.get_pg_cron_job() is None
    with connection.cursor() as cur:
        cur.execute(
            "select jobname from cron.job where jobname = %s", ["unrelated_job"]
        )
        assert cur.fetchone() is not None  # scoped: the unrelated job survives
        cur.execute("select cron.unschedule('unrelated_job')")  # don't leak it


def test_flush_absurd_state_drop_schema_false_never_touches_job_run_details() -> None:
    # runid is left to its default nextval (not hardcoded) and the count assertion is
    # relative (not absolute) — job_run_details is a global, cluster-wide pg_cron audit
    # table with no autouse cleanup anywhere in this project (deliberately, since
    # drop_schema=False must never touch it); a hardcoded runid or an absolute-count
    # assertion breaks the moment this test (or the suite) runs more than once against
    # the same --reuse-db database.
    with connection.cursor() as cur:
        cur.execute("select count(*) from cron.job_run_details")
        before = cur.fetchone()[0]
        cur.execute(
            "insert into cron.job_run_details (jobid, job_pid, database,"
            " username, command, status, return_message, start_time, end_time)"
            " values (0, 0, current_database(), current_user, 'select 1',"
            " 'succeeded', '', now(), now()) returning runid"
        )
        runid = cur.fetchone()[0]

    try:
        flush_absurd_state()

        with connection.cursor() as cur:
            cur.execute("select count(*) from cron.job_run_details")
            assert cur.fetchone()[0] == before + 1  # untouched
    finally:
        with connection.cursor() as cur:
            cur.execute("delete from cron.job_run_details where runid = %s", [runid])


def test_flush_absurd_state_pg_cron_schema_absent_is_silent() -> None:
    # Mirrors tests/core/test_checks.py::test_schema_absent_check_is_silent: an
    # unmigrated django_absurd_pg_cron schema must be a no-op, not a raise, for both
    # modes' pg_cron branch.
    call_command("migrate", "django_absurd_pg_cron", "zero", verbosity=0)
    try:
        flush_absurd_state()  # must not raise
        flush_absurd_state(drop_schema=True)  # must not raise
    finally:
        call_command("migrate", verbosity=0)  # restore django_absurd_scheduledtask
