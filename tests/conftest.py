import pytest
from django.core.exceptions import ImproperlyConfigured
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
