import typing as t

import pytest
import pytest_django.fixtures
from django.core.management import call_command

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.reconcile import (
    CLEANUP_COMMAND,
    CLEANUP_NAME,
    CLEANUP_SOURCE,
)
from tests.pg_cron import utils

pytestmark = pytest.mark.django_db(transaction=True)


def build_cleanup_tasks(cleanup_schedule: str) -> dict[str, dict[str, t.Any]]:
    tasks = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    tasks["default"]["OPTIONS"]["CLEANUP"] = {"schedule": cleanup_schedule}
    return tasks


def fetch_cleanup_lane() -> list[tuple[str, str, str, bool]]:
    return utils.fetch_managed_jobs(utils.fetch_live_database(), source=CLEANUP_SOURCE)


def build_cleanup_jobname() -> str:
    return catalog.build_jobname(
        utils.fetch_live_database(), CLEANUP_SOURCE, CLEANUP_NAME
    )


def test_sync_schedules_cleanup_job(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")

    assert fetch_cleanup_lane() == [
        (build_cleanup_jobname(), "17 * * * *", CLEANUP_COMMAND, True)
    ]


def test_cleanup_job_is_isolated_to_its_own_lane(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")

    live_db = utils.fetch_live_database()
    settings_jobs = utils.fetch_managed_jobs(live_db, source=Source.SETTINGS)
    admin_jobs = utils.fetch_managed_jobs(live_db, source=Source.ADMIN)
    assert [r[0] for r in settings_jobs] == []
    assert [r[0] for r in admin_jobs] == []
    assert [r[0] for r in fetch_cleanup_lane()] == [build_cleanup_jobname()]


def test_sync_unschedules_cleanup_job_when_cleanup_dropped(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")
    call_command("absurd_sync_crons")
    assert fetch_cleanup_lane() != []

    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    call_command("absurd_sync_crons")

    assert fetch_cleanup_lane() == []


def test_cleanup_job_survives_absurd_flush_command(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")

    call_command("absurd_sync_crons")
    call_command("absurd_sync_queues")
    assert fetch_cleanup_lane() != []

    call_command("absurd_flush", "--noinput")

    assert fetch_cleanup_lane() == [
        (build_cleanup_jobname(), "17 * * * *", CLEANUP_COMMAND, True)
    ]


def test_teardown_removes_cleanup_job(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_cleanup_tasks("17 * * * *")
    call_command("absurd_sync_crons")
    assert fetch_cleanup_lane() != []

    call_command("absurd_sync_crons", "--teardown", "--no-input")

    assert fetch_cleanup_lane() == []
