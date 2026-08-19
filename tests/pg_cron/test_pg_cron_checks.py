"""Static system-check tests against a real pg_cron backend."""

import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from pytest_django import Settings

from tests.utils import ABSURD_BACKEND, make_tasks_settings
from tests.utils import DECLARED_QUEUES as BASE_QUEUES

pytestmark = pytest.mark.django_db(transaction=True)


def run_pg_cron_check(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    options: dict[str, t.Any],
) -> str:
    """Drive check with given queues/schedule and return output.

    options keys: queues, schedule.
    """
    settings.TASKS = make_tasks_settings(
        queues=options["queues"], schedule=options["schedule"]
    )
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def run_pg_cron_cleanup_check(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    cleanup: dict[str, str],
) -> str:
    settings.TASKS = make_tasks_settings(queues=BASE_QUEUES, cleanup=cleanup)
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


@pytest.mark.parametrize("schedule", ["0 3 * * *", "30 seconds", "@daily"])
def test_pg_cron_cleanup_accepts_pg_cron_grammar(
    capsys: pytest.CaptureFixture[str],
    schedule: str,
    settings: Settings,
) -> None:
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": schedule})
    assert out == "System check identified no issues (0 silenced).\n"


def test_pg_cron_cleanup_rejects_a_non_cron_schedule(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": "not a cron"})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E010) django-absurd: invalid CLEANUP option. Expected a"
        " 5-field cron expression; got 3 fields.\n"
        "\tHINT: Use a 5-field cron expression, an interval such as"
        " '30 seconds' (1-59), or one of"
        " @hourly/@daily/@weekly/@monthly/@yearly/@annually/@midnight"
        " (lowercase). The beat scheduler's 6-field leading-seconds form is"
        " not pg_cron syntax.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_cleanup_rejects_empty_schedule(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": ""})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E010) django-absurd: invalid CLEANUP option.\n"
        "\tHINT: Set CLEANUP to a dict with a single 'schedule' key:"
        ' OPTIONS["CLEANUP"] = {"schedule": "<cron>"}.\n'
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_task_import_raise_reports_e007_not_crash(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """A scheduled task whose module raises non-ImportError on import.

    Must surface as E007, not crash `manage.py check` with a raw traceback.
    """
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {
                "boom": {
                    "task": "tests.raises_on_import.anything",
                    "cron": "0 2 * * *",
                },
            },
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'boom': task 'tests.raises_on_import.anything' could not be"
        " imported: RuntimeError('boom at import')\n"
        "\tHINT: Ensure the task path is importable and points to a"
        " @task-decorated function.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


@pytest.mark.parametrize("cron", ["30 seconds", "@daily", "0 2 * * *"])
def test_pg_cron_cron_grammar_accepted_at_check_time(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    cron: str,
) -> None:
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {"s": {"task": "tests.tasks.add", "cron": cron}},
        },
    )
    assert out == "System check identified no issues (0 silenced).\n"


def test_pg_cron_rejects_beats_six_field_cron_at_check_time(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    # The beat scheduler's leading-seconds form is not pg_cron syntax. pg_cron would
    # accept it and silently drop the sixth field — its parser reads five fields and
    # then the command — so the schedule would run hourly, not every 30 seconds.
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {"s": {"task": "tests.tasks.add", "cron": "*/30 * * * * *"}},
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 's':"
        " Expected a 5-field cron expression; got 6 fields.\n"
        "\tHINT: Use a 5-field cron expression, an interval such as"
        " '30 seconds' (1-59), or one of"
        " @hourly/@daily/@weekly/@monthly/@yearly/@annually/@midnight"
        " (lowercase). The beat scheduler's 6-field leading-seconds form is"
        " not pg_cron syntax.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_bad_name_charset_rejected(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Schedule name with spaces/special chars rejected under pg_cron."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {
                "bad name!": {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                }
            },
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'bad name!': Schedule name contains characters other than"
        " [A-Za-z0-9_-].\n"
        "\tHINT: Schedule names must match [A-Za-z0-9_-]+ when using the"
        " pg_cron scheduler.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_undeclared_task_queue_rejected(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Task with queue_name='reports' not in declared queues rejected."""
    # tests.tasks.on_reports has @task(queue_name="reports"); exclude from
    # declared queues so effective-queue check finds it undeclared. Still
    # include "other" (required by tasks module import) but omit "reports".
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": {"default": {}, "other": {}},
            "schedule": {
                "ghostly": {
                    "task": "tests.tasks.on_reports",
                    "cron": "0 2 * * *",
                    # no "queue" key — the check falls back to task.queue_name
                }
            },
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'ghostly': queue 'reports' is not declared.\n"
        "\tHINT: Declare the queue under OPTIONS['QUEUES'] or correct the"
        " queue name.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_undeclared_explicit_queue_single_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Undeclared explicit queue override yields exactly ONE E007 (core's)."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {
                "nightly": {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                    "queue": "ghost",
                }
            },
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly': queue 'ghost' is not declared.\n"
        "\tHINT: Declare the queue under OPTIONS['QUEUES'] or correct the"
        " queue name.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_non_mapping_schedule_single_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Non-mapping SCHEDULE under pg_cron yields only core's mapping E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": ["nightly"],
        },
    )
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


def test_pg_cron_non_mapping_entry_single_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Non-mapping schedule entry under pg_cron yields only core's E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {"nightly": "0 2 * * *"},
        },
    )
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


def test_pg_cron_missing_task_no_queue_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Missing task under pg_cron yields core's import E007 only."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {"nightly": {"cron": "0 2 * * *"}},
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly': task '' could not be imported: ImportError(\" doesn't"
        ' look like a module path")\n'
        "\tHINT: Ensure the task path is importable and points to a"
        " @task-decorated function.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_unimportable_task_no_queue_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Unimportable task under pg_cron yields core's import E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {"nightly": {"task": "tests.tasks.nope", "cron": "0 2 * * *"}},
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly': task 'tests.tasks.nope' could not be imported:"
        ' ImportError(\'Module "tests.tasks" does not define a "nope"'
        " attribute/class')\n"
        "\tHINT: Ensure the task path is importable and points to a"
        " @task-decorated function.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_non_task_no_queue_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Non-task path under pg_cron yields core's not-a-task E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {
                "nightly": {"task": "tests.tasks.Payload", "cron": "0 2 * * *"}
            },
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly': 'tests.tasks.Payload' is not a Django task.\n"
        "\tHINT: The path must point to a Django @task-decorated callable.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


@pytest.mark.parametrize("cron", ["", 300])
def test_pg_cron_structurally_absent_cron_rejected(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    cron: t.Any,
) -> None:
    """A missing or non-string cron is core's report, not the grammar check's — the
    grammar check defers to it so one field's problem is reported once."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {"nightly": {"task": "tests.tasks.add", "cron": cron}},
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly': cron must be a non-empty string.\n"
        "\tHINT: Set cron to a non-empty schedule string; the pg_cron app"
        " checks its grammar.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_trailing_newline_name_rejected(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Schedule name with trailing newline rejected (fullmatch, not match)."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {
                "nightly\n": {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                }
            },
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly\\n': Schedule name contains characters other than"
        " [A-Za-z0-9_-].\n"
        "\tHINT: Schedule names must match [A-Za-z0-9_-]+ when using the"
        " pg_cron scheduler.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_empty_string_queue_resolves_via_effective_queue(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """queue: "" is falsy — pg_cron resolves via task queue_name.

    Core raises one E007 (empty string is not a declared queue name); the
    pg_cron effective-queue check must not raise a second by treating ""
    as a declared-queue override rather than falling back to task.queue_name.
    """
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {
                "nightly": {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                    "queue": "",
                }
            },
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule"
        " 'nightly': queue '' is not declared.\n"
        "\tHINT: Declare the queue under OPTIONS['QUEUES'] or correct the"
        " queue name.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_valid_five_field_cron_no_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """Valid 5-field cron under pg_cron passes without absurd.E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {
                "nightly": {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                }
            },
        },
    )
    assert out == "System check identified no issues (0 silenced).\n"


def test_pg_cron_non_string_name_yields_e007_not_typeerror(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    """SCHEDULE key that is integer yields E007, not TypeError."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {5: {"task": "tests.tasks.add", "cron": "0 2 * * *"}},
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 5:"
        " Schedule name contains characters other than [A-Za-z0-9_-].\n"
        "\tHINT: Schedule names must match [A-Za-z0-9_-]+ when using the"
        " pg_cron scheduler.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_cleanup_cron_grammar_is_checked(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    # CLEANUP's cron is pg_cron's own grammar too, so it earns the same check as a
    # SCHEDULE entry: beat's 6-field form would otherwise reach sync, where pg_cron
    # accepts it and silently drops the sixth field.
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": "*/30 * * * * *"})
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E010) django-absurd: invalid CLEANUP option. Expected a"
        " 5-field cron expression; got 6 fields.\n"
        "\tHINT: Use a 5-field cron expression, an interval such as"
        " '30 seconds' (1-59), or one of"
        " @hourly/@daily/@weekly/@monthly/@yearly/@annually/@midnight"
        " (lowercase). The beat scheduler's 6-field leading-seconds form is"
        " not pg_cron syntax.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


@pytest.mark.parametrize("cleanup", [{"schedule": 5}, {"schedule": "   "}])
def test_pg_cron_cleanup_defers_a_non_grammar_problem_to_core(
    capsys: pytest.CaptureFixture[str],
    cleanup: dict[str, t.Any],
    settings: Settings,
) -> None:
    # Core already reports shape/emptiness as absurd.E010; reporting it again from the
    # grammar check would show one field's problem twice.
    out = run_pg_cron_cleanup_check(settings, capsys, cleanup)
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E010) django-absurd: invalid CLEANUP option.\n"
        "\tHINT: Set CLEANUP to a dict with a single 'schedule' key:"
        ' OPTIONS["CLEANUP"] = {"schedule": "<cron>"}.\n'
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_pg_cron_schedule_defers_an_empty_cron_to_core(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "queues": BASE_QUEUES,
            "schedule": {"s": {"task": "tests.tasks.add", "cron": "   "}},
        },
    )
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 's':"
        " cron must be a non-empty string.\n"
        "\tHINT: Set cron to a non-empty schedule string; the pg_cron app"
        " checks its grammar.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_queues_option_as_a_list_errors_without_crashing_the_schedule_check(
    settings: Settings,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD_BACKEND,
            "OPTIONS": {
                "QUEUES": ["default"],
                "SCHEDULE": {"s": {"task": "tests.tasks.add", "cron": "0 3 * * *"}},
            },
        }
    }
    with pytest.raises(SystemCheckError) as excinfo:
        call_command("check", "django_absurd")
    assert str(excinfo.value) == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E007) django-absurd: invalid SCHEDULE entry. Schedule 's':"
        " queue 'default' is not declared.\n"
        "\tHINT: Declare the queue under OPTIONS['QUEUES'] or correct the"
        " queue name.\n"
        "?: (absurd.E014) django-absurd: OPTIONS['QUEUES'] must be a mapping"
        " of queue name to policy options.\n"
        "\tHINT: Write OPTIONS['QUEUES'] = {'a': {}}, or declare names only"
        " with the top-level QUEUES list.\n"
        "\n"
        "System check identified 2 issues (0 silenced)."
    )
