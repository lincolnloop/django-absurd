import io
import typing as t

import pytest
from django.core.management import call_command

from django_absurd.flush import flush_absurd_state


@pytest.fixture(autouse=True)
def _enable_db(db: None) -> None:
    pass


@pytest.fixture
def _isolate_queues(_enable_db: None) -> t.Iterator[None]:
    """Hard-reset Absurd queue topology around a test (before AND after).

    Same role as the identically-named fixture in the repo-root ``tests/conftest.py``,
    but a separate definition rather than an inherited one: that conftest is a SIBLING
    of ``loadtest/``, not an ancestor, so ``--confcutdir=..`` never reaches it (the
    three real suites live under ``tests/`` and do inherit it).

    It also re-provisions, which the root fixture does not. The root version serves
    files that build their own topology in-test; the load harness has none — its
    queues come from ``post_migrate``, which a dropped schema does not re-run. Without
    the re-provision, dropping would leave every later test in the suite with no queue
    tables at all.
    """
    reset_queue_topology()
    yield
    reset_queue_topology()


def reset_queue_topology() -> None:
    flush_absurd_state(drop_schema=True)
    # Discarded stdout: the sync report is fixture bookkeeping. The seeder's own
    # progress lines are what a failing test should be showing.
    call_command("absurd_sync_queues", stdout=io.StringIO())
