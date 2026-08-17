import logging

import pytest

from django_absurd import params as params_module
from django_absurd.test import AbsurdTestRuntime
from tests import tasks


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
        f'spawn requested: name="{tasks.add.module_path}" queue="default"'
        " max_attempts=5"
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
        f'spawn requested: name="{tasks.add.module_path}" queue="default"'
        ' max_attempts=5 idempotency_key="café-42"'
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
) -> None:
    """A raising log call reaches the hook's own try/except, not the SDK's — a real
    enqueue still spawns the task, proving ``options`` came back unchanged even on the
    failing path. The fault is injected with a logging ``Filter``, which raises from
    inside ``Logger.handle``; it spares the fallback line so containment can report.
    """
    hooks_logger = logging.getLogger("django_absurd.hooks")
    hooks_logger.addFilter(fail_to_emit_the_spawn_line)
    try:
        with (
            caplog.at_level(logging.DEBUG, logger="django_absurd"),
            dj_absurd.freeze_time(),
        ):
            result = tasks.add.enqueue(1, 2)
            dj_absurd.drain()
    finally:
        hooks_logger.removeFilter(fail_to_emit_the_spawn_line)

    assert dj_absurd.get_result(result.id).state == "completed"
    failures = [
        r
        for r in caplog.records
        if r.name == "django_absurd.hooks" and r.levelno == logging.ERROR
    ]
    assert len(failures) == 1
    assert failures[0].getMessage() == (
        f'failed to log spawn: name="{tasks.add.module_path}"'
    )


def fail_to_emit_the_spawn_line(record: logging.LogRecord) -> bool:
    """Raise from inside ``Logger.handle`` for the spawn line only.

    A filter is the public seam for this: it runs inside ``Logger.handle``, before any
    handler, so raising here is what a project's own raising ``Filter`` would do to the
    log call. (A broken *formatter* would not — ``Handler.emit`` routes that to
    ``handleError``.) Letting every other record through keeps the fallback reportable.
    """
    if str(record.msg).startswith("spawn requested"):
        msg = "cannot emit the spawn line"
        raise ValueError(msg)
    return True
