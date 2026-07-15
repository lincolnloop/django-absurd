import pytest
from django.db import connection


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
