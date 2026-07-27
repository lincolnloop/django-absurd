import typing as t

import psycopg
import pytest
import pytest_django.fixtures
from django.db import ProgrammingError

from django_absurd import connection
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_open_central_connection_reaches_central_db(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({})
    with connection.open_central_connection("default") as cur:
        cur.execute("select current_database()")
        row = t.cast("tuple[str]", cur.fetchone())
    assert row[0] == connection.resolve_cron_database("default")


@pytest.mark.django_db(transaction=True)
def test_open_central_connection_translates_psycopg_errors(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({})
    with (
        pytest.raises(ProgrammingError) as excinfo,
        connection.open_central_connection("default") as cur,
    ):
        cur.execute("select * from this_table_does_not_exist")
    assert isinstance(excinfo.value.__cause__, psycopg.Error)
    assert getattr(excinfo.value.__cause__, "sqlstate", None) == "42P01"
