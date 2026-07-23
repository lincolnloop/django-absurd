import typing as t

import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd import ABSURD_SCHEMA_VERSION


def fetch_scalar(sql: str) -> t.Any:
    with connection.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


@pytest.mark.django_db
def test_migrate_installs_absurd_schema_at_pinned_version() -> None:
    assert fetch_scalar("SELECT to_regnamespace('absurd') IS NOT NULL") is True
    assert fetch_scalar("SELECT to_regclass('absurd.queues') IS NOT NULL") is True
    assert fetch_scalar("SELECT absurd.get_schema_version()") == ABSURD_SCHEMA_VERSION


@pytest.mark.django_db(transaction=True)
def test_reverse_drops_absurd_schema() -> None:
    call_command("migrate", "django_absurd", "zero", verbosity=0)
    assert fetch_scalar("SELECT to_regnamespace('absurd') IS NULL") is True
    call_command("migrate", verbosity=0)  # restore absurd schema
    assert fetch_scalar("SELECT absurd.get_schema_version()") == ABSURD_SCHEMA_VERSION
