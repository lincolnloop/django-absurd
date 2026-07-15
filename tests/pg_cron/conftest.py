import pytest
from django.db import connection


@pytest.fixture(autouse=True)
def _clear_pg_cron_jobs(request):
    """Unschedule every pg_cron job after the test — the test DB is ours, so blow the
    whole ``cron.job`` catalog away rather than namespacing to ``absurd:%``. Set-based
    via cron.unschedule (pg_cron's supported API — not a raw DELETE/TRUNCATE, which
    would desync the launcher). Skips tests without the ``django_db`` marker (they
    can't commit cron jobs, so there is nothing to unschedule).
    """
    yield
    if not request.node.get_closest_marker("django_db"):
        return
    with connection.cursor() as cur:
        cur.execute("select cron.unschedule(jobid) from cron.job")
