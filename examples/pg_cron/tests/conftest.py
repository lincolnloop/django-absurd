import pytest
from app import app
from django.conf import settings

# pg_cron only permits CREATE EXTENSION in the database named by the server's
# cron.database_name (the db_pg_cron compose service sets it to absurd_test_pg_cron).
# So the test database must use that exact name, not pytest-django's test_<db> default,
# or the pg_cron app's migration fails with "can only create extension in database …".
settings.DATABASES["default"].setdefault("TEST", {})
settings.DATABASES["default"]["TEST"]["NAME"] = "absurd_test_pg_cron"


@pytest.fixture(scope="session", autouse=True)
def _prepare_nanodjango() -> None:
    """Finish nanodjango's setup (admin/API routes) once per session — mirrors
    nanodjango's own (unmerged) example-app tests:
    https://github.com/radiac/nanodjango/pull/28."""
    app._prepare()
