import typing as t

import pytest
from django.db import connections


@pytest.fixture(autouse=True)
def _drop_connections_between_tests() -> t.Iterator[None]:
    """Hand no test a session an earlier one left mid-command.

    This suite is the only one that can be interrupted inside a wait: its tests run
    real probes and real drains, and the suite's own `pytest-timeout` alarm fires
    inside them. An interrupted wait leaves that connection stuck on
    `another command is already in progress`, and under `--reuse-db` the next test on
    the same worker inherits it — one timeout then reads as a whole worker's worth of
    unrelated failures. Closing costs a reconnect per test and bounds the damage to
    the test that was interrupted.
    """
    yield
    connections.close_all()
