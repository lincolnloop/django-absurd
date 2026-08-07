import pytest
from app import add  # app.py's own `add` task

from django_absurd.test import AbsurdTestRuntime


# transaction=True (not plain `db`): the `dj_absurd` fixture's drain opens its
# OWN dedicated DB connection (by design — see django_absurd/worker.py's
# aworker_client), separate from Django's. Under the default `db` fixture the
# test body runs inside an uncommitted atomic block, so the enqueue is invisible
# to that second connection and the task never gets claimed (stays READY).
# transaction=True commits for real, matching tests/core/test_pytest_plugin.py's
# own drain test.
@pytest.mark.django_db(transaction=True)
def test_add_task_completes_via_a_drain(dj_absurd: AbsurdTestRuntime) -> None:
    result = add.enqueue("2", "3")
    assert [run.state for run in dj_absurd.drain()] == ["completed"]
    result.refresh()
    assert result.status == "SUCCESSFUL"
