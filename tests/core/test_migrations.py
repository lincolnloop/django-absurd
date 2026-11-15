import typing as t

import pytest
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


@pytest.mark.django_db
def test_migrate_installs_no_extension() -> None:
    # Migrating must need no privilege beyond creating the schema, which holds only
    # while nothing here creates an extension: Absurd generates uuidv7 in PL/pgSQL,
    # falling back to pg_catalog on a server without it.
    installed = fetch_scalar(
        "SELECT array_agg(extname ORDER BY extname) FROM pg_extension"
    )
    assert installed == ["plpgsql"]
