import logging
import typing as t

import pytest

from django_absurd.hooks import log_before_spawn
from django_absurd.test import AbsurdTestRuntime
from tests import tasks

if t.TYPE_CHECKING:
    from absurd_sdk import SpawnOptions


@pytest.mark.django_db(transaction=True)
def test_enqueue_logs_the_spawn_with_absurd_side_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="django_absurd"):
        tasks.add.enqueue(1, 2)

    spawns = [r for r in caplog.records if r.name == "django_absurd.hooks"]
    assert len(spawns) == 1
    assert spawns[0].levelno == logging.DEBUG
    message = spawns[0].getMessage()
    assert tasks.add.module_path in message
    assert "queue=default" in message


@pytest.mark.django_db(transaction=True)
def test_a_spawn_still_works_with_the_hook_attached(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """before_spawn's return value IS the spawn options; a hook returning None
    would break every spawn."""
    with dj_absurd.freeze_time():
        result = tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    assert dj_absurd.get_result(result.id).state == "completed"


def test_a_hook_that_fails_to_log_still_returns_the_options(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """describe_spawn interpolates the queue directly; a queue value that raises on
    ``str()`` reaches the hook's own try/except, not the SDK's."""

    class Unstringable:
        def __str__(self) -> t.Never:
            msg = "cannot stringify"
            raise ValueError(msg)

    options = t.cast("SpawnOptions", {"queue": Unstringable()})
    with caplog.at_level(logging.DEBUG, logger="django_absurd"):
        result = log_before_spawn("tests.tasks.add", None, options)

    assert result is options
    failures = [r for r in caplog.records if r.name == "django_absurd.hooks"]
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert failures[0].getMessage() == "failed to log spawn: name=tests.tasks.add"
