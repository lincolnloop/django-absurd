import typing as t

import pytest
from django.db import connections

import analysis
from django_absurd.queues import resolve_absurd_database
from django_absurd.test import open_test_connection


@pytest.fixture(autouse=True)
def _drop_what_a_test_leaves_behind() -> t.Iterator[None]:
    """Hand no test a session or a probe table an earlier one left behind.

    This suite is the only one that can be interrupted inside a wait: its tests run
    real probes and real drains, and the suite's own `pytest-timeout` alarm fires
    inside them. An interrupted wait leaves that connection stuck on
    `another command is already in progress`, and under `--reuse-db` the next test on
    the same worker inherits it — one timeout then reads as a whole worker's worth of
    unrelated failures. Closing costs a reconnect per test and bounds the damage to
    the test that was interrupted.

    The commit-ceiling probe's table is the same hazard in another form:
    `analysis.measure_commit_ceiling` drops it on the way out of a probe that FINISHED
    and deliberately never from a `finally`, so an interrupted or refused probe leaves
    the name taken — and a probe reads a name it does not own as a refusal, in every
    later run too, since `--reuse-db` hands the same database on. Dropped on a
    connection of its own, which is what makes it safe here: Django's is either stuck
    mid-command or just closed inside the test's own atomic block, and both raise over
    a statement issued now.
    """
    yield
    connections.close_all()
    with open_test_connection(resolve_absurd_database()) as cursor:
        cursor.execute(f"drop table if exists {analysis.COMMIT_PROBE_TABLE}")
