import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.queues import get_absurd_client


@pytest.fixture(autouse=True)
def _enable_db(db: None) -> None:
    pass


@pytest.fixture(autouse=True)
def _reset_absurd_queues(_enable_db: None) -> None:
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


@pytest.fixture
def admin_user() -> User:
    return User.objects.create_superuser("admin", "a@x.com", "pw")


@pytest.fixture
def staff_user() -> User:
    return User.objects.create_user("staff", "s@x.com", "pw", is_staff=True)
