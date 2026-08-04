import logging
import typing as t

import pytest

from django_absurd import hooks
from django_absurd import params as params_module
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
    assert spawns[0].getMessage() == (
        f"spawn requested: name={tasks.add.module_path} queue=default max_attempts=5"
    )


@pytest.mark.django_db(transaction=True)
def test_enqueue_logs_a_dedup_key_as_the_caller_wrote_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key is the caller's own string, logged verbatim. Escaping it to ASCII would
    mangle an ordinary accented key to defend a stream encoding you can only get by
    deliberately breaking your own environment."""
    with caplog.at_level(logging.DEBUG, logger="django_absurd"):
        params_module.absurd_params(idempotency_key="café-42").bind(tasks.add).enqueue(
            1, 2
        )

    spawns = [r for r in caplog.records if r.name == "django_absurd.hooks"]
    assert len(spawns) == 1
    assert spawns[0].getMessage() == (
        f"spawn requested: name={tasks.add.module_path} queue=default"
        " max_attempts=5 idempotency_key=café-42"
    )


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


@pytest.mark.django_db(transaction=True)
def test_a_hook_that_fails_to_log_still_returns_the_options(
    caplog: pytest.LogCaptureFixture,
    dj_absurd: AbsurdTestRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """describe_spawn is the message-building step; breaking it reaches the hook's own
    try/except, not the SDK's — a real enqueue still spawns the task, proving
    ``options`` came back unchanged even on the failing path."""

    def explode(task_name: str, options: "SpawnOptions") -> str:
        msg = "cannot describe spawn"
        raise ValueError(msg)

    # Sanctioned exception to tests/CLAUDE.md's no-monkeypatching rule: the branch under
    # test is the hook's own containment, which by definition no real input can reach —
    # a fault has to be injected. The maintainer authorised it for these two tests only.
    monkeypatch.setattr(hooks, "describe_spawn", explode)

    with (
        caplog.at_level(logging.DEBUG, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    assert dj_absurd.get_result(result.id).state == "completed"
    failures = [
        r
        for r in caplog.records
        if r.name == "django_absurd.hooks" and r.levelno == logging.ERROR
    ]
    assert len(failures) == 1
    assert failures[0].getMessage() == (
        f"failed to log spawn: name={tasks.add.module_path}"
    )
