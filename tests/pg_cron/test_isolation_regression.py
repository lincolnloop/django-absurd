# The runtime no-leak proof (the live experiment, made deterministic). NOT marked slow —
# it RUNS in CI (no deselected tests). Determinism comes from a positive sync point
# (poll cron.job_run_details until the producer fires into its own DB) rather than a
# blind sleep; xdist-safe because the producer targets a per-worker-unique DB.
import pytest
import pytest_django.fixtures
from django.db import connections

from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_producing_schedule_never_fires_into_this_test_db(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    test_db = str(connections["default"].settings_dict["NAME"])
    # a task-producing cron bound to a NON-test DB (unique per worker), enqueuing/second
    producer = utils.schedule_producer_cron(target=utils.scratch_db_name())
    try:
        # positive sync: the producer fired into its OWN (scratch) DB
        utils.wait_for_fire(producer, timeout=20)
        # nothing leaked into THIS test DB
        assert utils.absurd_queue_depth(test_db) == 0
    finally:
        utils.remove_producer(producer)


def test_wait_for_fire_raises_when_producer_never_succeeds() -> None:
    """The positive-sync poll fails loudly (rather than falling through to a false
    ``== 0`` pass) when the producer never records a succeeded run."""
    producer = utils.Producer(jobid=-1, target="absurd_scratch_never")
    with pytest.raises(AssertionError) as excinfo:
        utils.wait_for_fire(producer, timeout=0.0)
    assert str(excinfo.value) == (
        "producer cron job -1 (target 'absurd_scratch_never') never succeeded "
        "within 0.0s — the pg_cron launcher did not complete a cycle, so the "
        "no-leak assertion cannot be trusted."
    )
