"""Static system-check tests against a real pg_cron backend."""

import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from pytest_django import Settings

from tests.utils import ABSURD_BACKEND, make_tasks_settings
from tests.utils import DECLARED_QUEUES as BASE_QUEUES

pytestmark = pytest.mark.django_db(transaction=True)

E007_MSG = "django-absurd: invalid SCHEDULE entry."


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
    assert "absurd.E010" not in out


def test_pg_cron_cleanup_rejects_a_non_cron_schedule(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": "not a cron"})
    assert "absurd.E010" in out


def test_pg_cron_cleanup_rejects_empty_schedule(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": ""})
    assert "absurd.E010" in out


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
    assert E007_MSG in out
    assert "could not be imported" in out


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
    assert "absurd.E007" not in out


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
    assert "absurd.E007" in out
    assert "Expected a 5-field cron expression; got 6 fields." in out


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
    assert "absurd.E007" in out
    assert "Schedule name contains characters other than [A-Za-z0-9_-]." in out


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
    assert "absurd.E007" in out
    assert "queue 'reports' is not declared" in out


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
    assert "absurd.E007" in out
    assert out.count("queue 'ghost' is not declared") == 1


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
    assert out.count('OPTIONS["SCHEDULE"] must be a mapping of name -> spec') == 1


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
    assert out.count("Schedule 'nightly' must be a mapping.") == 1


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
    assert "could not be imported" in out
    assert "is not declared" not in out


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
    assert "could not be imported" in out
    assert "is not declared" not in out


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
    assert "is not a Django task" in out
    assert "is not declared" not in out


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
    assert "absurd.E007" in out
    assert "cron must be a non-empty string." in out


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
    assert "absurd.E007" in out
    assert "Schedule name contains characters other than [A-Za-z0-9_-]." in out


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
    assert out.count("queue '' is not declared") == 1


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
    assert "absurd.E007" not in out


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
    assert "absurd.E007" in out
    assert "TypeError" not in out


def test_pg_cron_cleanup_cron_grammar_is_checked(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    # CLEANUP's cron is pg_cron's own grammar too, so it earns the same check as a
    # SCHEDULE entry: beat's 6-field form would otherwise reach sync, where pg_cron
    # accepts it and silently drops the sixth field.
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": "*/30 * * * * *"})
    assert "absurd.E010" in out
    assert "Expected a 5-field cron expression; got 6 fields." in out


@pytest.mark.parametrize("cleanup", [{"schedule": 5}, {"schedule": "   "}])
def test_pg_cron_cleanup_defers_a_non_grammar_problem_to_core(
    capsys: pytest.CaptureFixture[str],
    cleanup: dict[str, t.Any],
    settings: Settings,
) -> None:
    # Core already reports shape/emptiness as absurd.E010; reporting it again from the
    # grammar check would show one field's problem twice.
    out = run_pg_cron_cleanup_check(settings, capsys, cleanup)
    assert out.count("absurd.E010") == 1


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
    assert out.count("absurd.E007") == 1


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
    assert "absurd.E014" in str(excinfo.value)
    assert (
        "django-absurd: OPTIONS['QUEUES'] must be a mapping of queue name to"
        " policy options." in str(excinfo.value)
    )
