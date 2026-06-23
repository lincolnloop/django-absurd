import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.queues import get_absurd_client


@pytest.fixture(autouse=True)
def _enable_db(db):
    pass


@pytest.fixture(autouse=True)
def _reset_absurd_queues(_enable_db):
    try:
        client = get_absurd_client()
        for name in client.list_queues():
            client.drop_queue(name)
    except (OperationalError, ProgrammingError, ImproperlyConfigured):
        pass
