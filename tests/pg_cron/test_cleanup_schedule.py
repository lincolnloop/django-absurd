import typing as t

import pytest
import pytest_django.fixtures
from django.core.management import call_command
from django.db import connection

from django_absurd.pg_cron.models import ScheduledTask

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_cleanup_tasks(cleanup_schedule: str) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "CLEANUP": {"schedule": cleanup_schedule},
            },
        }
    }


def fetch_cleanup_row() -> tuple[str, str, str] | None:
    with connection.cursor() as cur:
        cur.execute(
            "select jobname, schedule, command from cron.job where jobname = %s",
            ["absurd_cleanup_all"],
        )
        row = cur.fetchone()
        return t.cast("tuple[str, str, str] | None", row)


def test_sync_schedules_absurd_cleanup_all_job(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")

    assert fetch_cleanup_row() == (
        "absurd_cleanup_all",
        "17 * * * *",
        "select * from absurd.cleanup_all_queues(null::text);",
    )


def test_cleanup_job_is_outside_managed_namespace(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")

    assert ScheduledTask.pg_cron.get_managed_jobs() == []


def test_sync_unschedules_cleanup_job_when_cleanup_dropped(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")
    call_command("absurd_sync_crons")
    assert fetch_cleanup_row() is not None

    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
            },
        }
    }
    call_command("absurd_sync_crons")

    assert fetch_cleanup_row() is None


def test_cleanup_all_job_survives_flush(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
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


def test_teardown_removes_cleanup_job(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")
    call_command("absurd_sync_crons")
    assert fetch_cleanup_row() is not None

    call_command("absurd_sync_crons", "--teardown", "--no-input")

    assert fetch_cleanup_row() is None


def test_teardown_reclaims_cleanup_job_without_cleanup_option(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
            },
        }
    }
    with connection.cursor() as cur:
        cur.execute(
            "select cron.schedule(%s, %s, %s)",
            ["absurd_cleanup_all", "5 * * * *", "select 1"],
        )
    assert fetch_cleanup_row() is not None

    call_command("absurd_sync_crons", "--teardown", "--no-input")

    assert fetch_cleanup_row() is None
