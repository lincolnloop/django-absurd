import typing as t

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError

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
def test_unapplying_a_delta_migration_is_refused() -> None:
    # Absurd publishes no downgrade SQL, so every delta is irreversible and the whole
    # chain unapplies no further than the delta on top of it. Nothing in this package
    # may reach for `migrate django_absurd zero`; a test wanting the schema gone renames
    # it (tests.utils.hide_absurd_schema) instead.
    with pytest.raises(IrreversibleError):
        call_command("migrate", "django_absurd", "0001", verbosity=0)
    assert fetch_scalar("SELECT absurd.get_schema_version()") == ABSURD_SCHEMA_VERSION
