import logging

import pytest

from django_absurd.test import AbsurdTestRuntime
from tests import tasks


@pytest.mark.django_db(transaction=True)
def test_a_drain_logs_under_the_module_loggers_that_own_its_events(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """Equality, not a subset: this is the canary for a module logging noise.

    ``worker`` reports the worker itself starting, ``hooks`` reports the run's own
    lifecycle from ``wrap_task_execution``. Nothing else has anything to say about
    enqueuing and running one task, and a new name appearing here should be a decision,
    not a surprise.
    """
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    names = {r.name for r in caplog.records if r.name.startswith("django_absurd")}
    assert names == {"django_absurd.hooks", "django_absurd.worker"}


@pytest.mark.django_db(transaction=True)
def test_no_message_repeats_the_package_name(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """%(name)s already carries it; a hand-written prefix duplicates it."""
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    ours = [r for r in caplog.records if r.name.startswith("django_absurd")]
    assert ours
    assert [r for r in ours if "django-absurd" in r.getMessage()] == []
