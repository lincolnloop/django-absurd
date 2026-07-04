import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.queues import get_absurd_client


@pytest.fixture(autouse=True)
def _enable_db(db):
    pass


@pytest.fixture(autouse=True)
def _reset_absurd_queues(_enable_db):
    """Drop all Absurd queues before each test.

    ``transaction=True`` tests create queues whose per-queue tables (DDL) and
    ``managed=False`` registry rows are not rolled back / flushed, so they leak
    across ``--reuse-db`` runs. Reset to zero queues so every test is hermetic.
    """
    try:
        client = get_absurd_client()
        for name in client.list_queues():
            client.drop_queue(name)
    except (OperationalError, ProgrammingError, ImproperlyConfigured):
        pass  # absurd schema not present (unmigrated / schema-absent test)


@pytest.fixture(scope="session")
def ensure_pgcron(django_db_setup, django_db_blocker):
    """Enable ``pg_cron`` on the test DB for pgcron-marked tests.

    Non-autouse and opt-in via ``pytest.mark.usefixtures("ensure_pgcron")`` so
    the default ``uv run pytest`` (which deselects ``pgcron``) never needs the
    extension. ``CREATE EXTENSION pg_cron`` is only permitted in the DB named by
    ``cron.database_name``; ``tests/settings.py`` pins the test DB to that name.
    """
    with django_db_blocker.unblock(), connection.cursor() as cur:
        cur.execute("create extension if not exists pg_cron")


@pytest.fixture
def admin_user(_enable_db):
    return get_user_model().objects.create_superuser("admin", "a@x.com", "pw")


@pytest.fixture
def staff_user(_enable_db):
    return get_user_model().objects.create_user("staff", "s@x.com", "pw", is_staff=True)
