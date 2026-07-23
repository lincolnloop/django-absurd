import typing as t

import pytest
from app import add  # app.py's own `add` task


# transaction=True (not plain `db`): absurd_drain_queue's burst worker opens its
# OWN dedicated DB connection (by design — see django_absurd/worker.py's
# aworker_client), separate from Django's. Under the default `db` fixture the
# test body runs inside an uncommitted atomic block, so the enqueue is invisible
# to that second connection and the task never gets claimed (stays READY).
# transaction=True commits for real, matching tests/core/test_pytest_plugin.py's
# own drain-queue test.
@pytest.mark.django_db(transaction=True)
def test_add_task_completes_via_absurd_drain_queue(
    absurd_drain_queue: t.Callable[..., None],
) -> None:
    result = add.enqueue("2", "3")
    absurd_drain_queue()
    result.refresh()
    assert result.status == "SUCCESSFUL"
