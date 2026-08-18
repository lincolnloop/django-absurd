import pytest
from django.db import connections
from pytest_django import Settings

from django_absurd.flush import flush_absurd_state
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from tests.pg_cron import utils

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
