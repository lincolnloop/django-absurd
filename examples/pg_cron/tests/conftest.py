import pytest
from app import app
from django.conf import settings


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings: None) -> None:
    """Pin the test database name to the server's ``cron.database_name``.

    pg_cron only permits ``CREATE EXTENSION`` in the database named by
    ``cron.database_name`` (a single server-level value; the db_pg_cron compose
    service sets it to ``absurd_test_pg_cron``), so the test database must use that
    exact name — not pytest-django's ``test_<db>`` default — or this app's migration
    fails with "can only create extension in database …". The name is necessarily
    shared with the internal ``tests/pg_cron`` suite: whichever suite last ran
    ``--create-db`` owns the schema on a given server.
    """
    settings.DATABASES["default"].setdefault("TEST", {})
    settings.DATABASES["default"]["TEST"]["NAME"] = "absurd_test_pg_cron"


@pytest.fixture(scope="session", autouse=True)
def _prepare_nanodjango() -> None:
    """Finish nanodjango's setup (admin/API routes) once per session — mirrors
    nanodjango's own (unmerged) example-app tests:
    https://github.com/radiac/nanodjango/pull/28."""
    app._prepare()
