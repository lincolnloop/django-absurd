import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.pg_cron.models import ScheduledTask

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_cleanup_tasks(cleanup_schedule):
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "pg_cron",
                "CLEANUP": {"schedule": cleanup_schedule},
            },
        }
    }


def fetch_cleanup_row():
    with connection.cursor() as cur:
        cur.execute(
            "select jobname, schedule, command from cron.job where jobname = %s",
            ["absurd_cleanup_all"],
        )
        return cur.fetchone()


def test_sync_schedules_absurd_cleanup_all_job(settings):
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")

    assert fetch_cleanup_row() == (
        "absurd_cleanup_all",
        "17 * * * *",
        "select * from absurd.cleanup_all_queues(null::text);",
    )


def test_cleanup_job_is_outside_managed_namespace(settings):
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")

    assert ScheduledTask.pg_cron.get_managed_jobs() == []


def test_sync_unschedules_cleanup_job_when_cleanup_dropped(settings):
    settings.TASKS = build_cleanup_tasks("17 * * * *")
    call_command("absurd_sync_crons")
    assert fetch_cleanup_row() is not None

    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "pg_cron",
            },
        }
    }
    call_command("absurd_sync_crons")

    assert fetch_cleanup_row() is None


def test_cleanup_all_job_survives_flush(settings):
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")
    call_command("absurd_sync_queues")
    assert fetch_cleanup_row() is not None

    call_command("absurd_flush", "--noinput")

    assert fetch_cleanup_row() == (
        "absurd_cleanup_all",
        "17 * * * *",
        "select * from absurd.cleanup_all_queues(null::text);",
    )


def test_teardown_removes_cleanup_job(settings):
    settings.TASKS = build_cleanup_tasks("17 * * * *")
    call_command("absurd_sync_crons")
    assert fetch_cleanup_row() is not None

    call_command("absurd_sync_crons", "--teardown", "--no-input")

    assert fetch_cleanup_row() is None
