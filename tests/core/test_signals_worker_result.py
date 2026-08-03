import pytest

from django_absurd.test import AbsurdTestRuntime
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_worker_result_id_round_trips_and_names_the_backend(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = tasks.report_context.enqueue()
    dj_absurd.drain()

    payload = dj_absurd.get_result(result.id).result
    assert payload is not None
    assert payload["id"] == result.id
    assert payload["id"].startswith("default:")
    assert payload["backend"] == "default"
    snapshot = dj_absurd.get_result(payload["id"])
    assert f"{snapshot.queue}:{snapshot.task_id}" == result.id
