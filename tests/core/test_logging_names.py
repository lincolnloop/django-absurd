import logging

import pytest

from django_absurd.test import AbsurdTestRuntime
from tests import tasks


@pytest.mark.django_db(transaction=True)
def test_worker_logs_under_its_own_module_logger(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    names = {r.name for r in caplog.records if r.name.startswith("django_absurd")}
    assert names == {"django_absurd.worker"}


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
