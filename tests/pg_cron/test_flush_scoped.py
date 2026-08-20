import pytest
from django.core.management import call_command
from django.db import connections
from pytest_django import Settings

from django_absurd.flush import flush_absurd_state
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.queues import list_provisioned_queues
from tests.pg_cron import utils
from tests.utils import answer

pytestmark = pytest.mark.django_db(transaction=True)


def test_flush_only_removes_this_database_jobs(settings: Settings) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    live_db = str(connections["default"].settings_dict["NAME"])
    catalog.schedule_job(
        "default",
        name="mine",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    utils.schedule_control_job_in_other_database("other_db_name")
    try:
        flush_absurd_state()

        mine = catalog.build_jobname(live_db, Source.SETTINGS, "mine")
        assert utils.fetch_cron_job(mine) is None
        assert utils.is_control_job_present("other_db_name") is True
    finally:
        utils.remove_control_job("other_db_name")


@pytest.mark.usefixtures("_isolate_queues")
def test_flush_command_removes_owned_pg_cron_jobs_and_rows(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    ScheduledTask.objects.create(
        name="admin-job",
        source=Source.ADMIN,
        task="tests.tasks.add",
        cron="0 4 * * *",
    )
    call_command("absurd_sync_crons")
    call_command("absurd_sync_queues")
    assert utils.fetch_managed_jobs(utils.fetch_live_database()) != []
    capsys.readouterr()  # discard sync output

    with answer("yes\n"):
        call_command("absurd_flush")

    assert capsys.readouterr().out == (
        "This will DROP 3 queue(s) and ALL their data: default, other, reports\n"
        "This will also UNSCHEDULE django-absurd's pg_cron jobs and delete ALL"
        " schedule rows, including admin-authored ones.\n"
        "Type 'yes' to continue, or 'no' to cancel: "
        "Unscheduled django-absurd's pg_cron jobs and removed 2 schedule row(s).\n"
        "Dropped 3 queue(s): default, other, reports\n"
        "Re-provision with: manage.py absurd_sync_queues,"
        " manage.py absurd_sync_crons\n"
    )
    assert utils.fetch_managed_jobs(utils.fetch_live_database()) == []
    assert ScheduledTask.objects.exists() is False


@pytest.mark.usefixtures("_isolate_queues")
def test_flush_command_clears_pg_cron_state_with_no_queues_provisioned(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons")
    assert list_provisioned_queues() == []
    capsys.readouterr()  # discard sync output

    with answer("yes\n"):
        call_command("absurd_flush")

    assert capsys.readouterr().out == (
        "This will also UNSCHEDULE django-absurd's pg_cron jobs and delete ALL"
        " schedule rows, including admin-authored ones.\n"
        "Type 'yes' to continue, or 'no' to cancel: "
        "Unscheduled django-absurd's pg_cron jobs and removed 1 schedule row(s).\n"
        "Re-provision with: manage.py absurd_sync_queues,"
        " manage.py absurd_sync_crons\n"
    )
    assert utils.fetch_managed_jobs(utils.fetch_live_database()) == []
