import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from pytest_django import Settings

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


@pytest.fixture
def run_check(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> t.Callable[[t.Any], str]:
    def _run(schedule: t.Any) -> str:
        settings.TASKS = {
            "default": {
                "BACKEND": ABSURD,
                "OPTIONS": {
                    "QUEUES": {"default": {}, "other": {}},
                    "SCHEDULE": schedule,
                },
            }
        }
        try:
            call_command("check", "django_absurd")
        except SystemCheckError as exc:
            cap = capsys.readouterr()
            return cap.out + cap.err + str(exc)
        cap = capsys.readouterr()
        return cap.out + cap.err

    return _run


def test_valid_schedule_no_error(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check({"ok": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    assert out == "System check identified no issues (0 silenced).\n"


def test_unimportable_task(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check({"x": {"task": "tests.tasks.nope", "cron": "0 2 * * *"}})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " task 'tests.tasks.nope' could not be imported: ImportError('Module"
        ' "tests.tasks" does not define a "nope" attribute/class\')\n'
        "\tHINT: Ensure the task path is importable and points to a"
        " @task-decorated function.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_non_import_error_at_task_import(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check(
        {"x": {"task": "tests.raises_on_import.anything", "cron": "0 2 * * *"}}
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " task 'tests.raises_on_import.anything' could not be imported:"
        " RuntimeError('boom at import')\n"
        "\tHINT: Ensure the task path is importable and points to a"
        " @task-decorated function.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_schedule_not_a_mapping(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check(["nightly"])
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry."
        ' OPTIONS["SCHEDULE"] must be a mapping of name -> spec.\n'
        "\tHINT: Set SCHEDULE to a dict mapping schedule names to spec"
        " dicts.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_schedule_entry_not_a_mapping(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check({"nightly": "0 2 * * *"})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly' must be a mapping.\n"
        "\tHINT: Set the schedule entry to a dict with task, cron, and"
        " optional queue/args/kwargs.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_not_a_task(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check({"x": {"task": "tests.tasks.Payload", "cron": "0 2 * * *"}})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " 'tests.tasks.Payload' is not a Django task.\n"
        "\tHINT: The path must point to a Django @task-decorated callable.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_bad_cron(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check({"x": {"task": "tests.tasks.add", "cron": "not-cron"}})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " invalid cron expression 'not-cron'.\n"
        "\tHINT: Provide a valid cron expression (e.g. '0 2 * * *').\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_non_string_cron(run_check: t.Callable[[t.Any], str]) -> None:
    # A non-string cron (e.g. forgot the quotes) must yield a clean E007, not an
    # AttributeError from croniter.is_valid — the check runs at worker/beat boot.
    out = run_check({"x": {"task": "tests.tasks.add", "cron": 300}})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " invalid cron expression 300.\n"
        "\tHINT: Provide a valid cron expression (e.g. '0 2 * * *').\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_beat_rejects_pg_cron_interval_syntax(
    run_check: t.Callable[[t.Any], str],
) -> None:
    # "[1-59] seconds" is pg_cron's grammar; under beat, croniter rejects it.
    out = run_check({"x": {"task": "tests.tasks.add", "cron": "30 seconds"}})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " invalid cron expression '30 seconds'.\n"
        "\tHINT: Provide a valid cron expression (e.g. '0 2 * * *').\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_unknown_key(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check({"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "bogus": 1}})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " unknown key 'bogus'.\n"
        "\tHINT: Remove unknown keys; valid keys are: task, cron, queue, args,"
        " kwargs.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_non_serializable_args(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "args": [object()]}}
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " args is not JSON-serializable.\n"
        "\tHINT: Ensure args and kwargs contain only JSON-serializable"
        " values.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_undeclared_queue(run_check: t.Callable[[t.Any], str]) -> None:
    out = run_check(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "queue": "ghost"}}
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " queue 'ghost' is not declared.\n"
        "\tHINT: Declare the queue under OPTIONS['QUEUES'] or correct the"
        " queue name.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_non_string_queue(run_check: t.Callable[[t.Any], str]) -> None:
    # A non-string queue (e.g. a list) must yield a clean E007, not a TypeError
    # from the `queue not in declared_queues` membership test.
    out = run_check(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "queue": ["bad"]}}
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 'x':"
        " queue ['bad'] is not declared.\n"
        "\tHINT: Declare the queue under OPTIONS['QUEUES'] or correct the"
        " queue name.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )
