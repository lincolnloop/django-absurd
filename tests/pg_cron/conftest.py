import pytest
from asgiref.local import Local
from django.db import connection
from django.tasks import task_backends

from tests.fixtures import (  # noqa: F401
    _enable_db,
    _reset_absurd_queues,
    admin_user,
    staff_user,
)


@pytest.fixture(autouse=True)
def _reset_task_backends():
    """Blow away the task-backend cache before each test so a mutated ``settings.TASKS``
    can't leak a stale backend into the next test's task resolution.

    ``TaskBackendHandler`` caches the ``TASKS`` setting (a cached_property) and each
    created backend (in ``_connections``); Django 6.0's test setting_changed receivers
    do NOT reset it, so a stale backend would bind tasks to the wrong queues and make
    task resolution non-deterministic across tests."""
    task_backends._connections = Local(task_backends.thread_critical)
    task_backends.__dict__.pop("settings", None)


@pytest.fixture(autouse=True)
def _clear_owned_pg_cron_jobs(request):
    """Unschedule all ``absurd:%`` pg_cron jobs after the test.

    The broad ``absurd:%`` pattern catches every job created during a test, not just
    a per-alias prefix. Skips tests without the ``django_db`` marker (they can't
    commit cron jobs, so there is nothing to unschedule).
    """
    yield
    if not request.node.get_closest_marker("django_db"):
        return
    with connection.cursor() as cur:
        cur.execute("select jobid from cron.job where jobname like 'absurd:%'")
        for (jobid,) in cur.fetchall():
            cur.execute("select cron.unschedule(%s)", [jobid])
