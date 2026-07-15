import pytest
from asgiref.local import Local
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import OperationalError, ProgrammingError
from django.tasks import task_backends

from django_absurd.queues import get_absurd_client


@pytest.fixture(autouse=True)
def _reset_task_backends():
    """Blow away the task-backend cache before each test so a mutated ``settings.TASKS``
    can't leak a stale backend into the next test's task resolution.

    ``TaskBackendHandler`` caches the ``TASKS`` setting (a cached_property) and each
    created backend (in ``_connections``); Django 6.0's test setting_changed receivers
    do NOT reset it, so a stale backend would bind tasks to the wrong queues and make
    task resolution non-deterministic across tests. Every suite mutates
    ``settings.TASKS``, so this reset lives at the shared parent conftest.
    """
    task_backends._connections = Local(task_backends.thread_critical)
    task_backends.__dict__.pop("settings", None)


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


@pytest.fixture
def admin_user(_enable_db):
    return get_user_model().objects.create_superuser("admin", "a@x.com", "pw")


@pytest.fixture
def staff_user(_enable_db):
    return get_user_model().objects.create_user("staff", "s@x.com", "pw", is_staff=True)
