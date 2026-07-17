"""Static E007 checks for SCHEDULER="pg_cron" entries."""

import typing as t

import pytest
import pytest_django.fixtures
from django.core.management import call_command
from django.core.management.base import SystemCheckError

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"
E007_MSG = "django-absurd: invalid SCHEDULE entry."

# tests/tasks.py declares @task(queue_name="other") and @task(queue_name="reports")
# at module level; importing any task from that module validates those queue names
# against the current backend. All tests that import from tests.tasks must
# therefore declare at least "other" and "reports" alongside "default".
BASE_QUEUES: dict[str, dict[str, t.Any]] = {
    "default": {},
    "other": {},
    "reports": {},
}


def run_pg_cron_check(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
    options: dict[str, t.Any],
) -> str:
    """Drive check with given scheduler/queues/schedule and return output.

    options keys: scheduler, alias (default "default"), queues, schedule.
    """
    alias = options.get("alias", "default")
    settings.TASKS = {
        alias: {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "SCHEDULER": options["scheduler"],
                "QUEUES": options["queues"],
                "SCHEDULE": options["schedule"],
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


def test_pg_cron_task_import_raise_reports_e007_not_crash(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A scheduled task whose module raises non-ImportError on import.

    Must surface as E007, not crash `manage.py check` with a raw traceback.
    """
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
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


@pytest.mark.parametrize("cron", ["*/30 * * * * *", "30 seconds"])
def test_pg_cron_cron_grammar_not_checked(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
    cron: str,
) -> None:
    """pg_cron cron grammar is DB-authoritative.

    Neither '[1-59] seconds' interval nor 6-field expression rejected at check
    time — cron.schedule validates at sync (croniter is beat-only validator).
    """
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {"s": {"task": "tests.tasks.add", "cron": cron}},
        },
    )
    assert "absurd.E007" not in out


def test_pg_cron_bad_name_charset_rejected(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schedule name with spaces/special chars rejected under pg_cron."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
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


def test_pg_cron_jobname_too_long_rejected(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Composed jobname exceeding 63 bytes rejected under pg_cron."""
    # alias "default" + name long enough to push absurd:s:default:<name> > 63 bytes
    # "absurd:s:default:" = 17 chars, so name needs > 46 chars
    long_name = "a" * 47
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {
                long_name: {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                }
            },
        },
    )
    assert "absurd.E007" in out
    assert "job name exceeds 63 bytes" in out


def test_pg_cron_undeclared_task_queue_rejected(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Task with queue_name='reports' not in declared queues rejected."""
    # tests.tasks.on_reports has @task(queue_name="reports"); exclude from
    # declared queues so effective-queue check finds it undeclared. Still
    # include "other" (required by tasks module import) but omit "reports".
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
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
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Undeclared explicit queue override yields exactly ONE E007 (core's)."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
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
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-mapping SCHEDULE under pg_cron yields only core's mapping E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": ["nightly"],
        },
    )
    assert out.count('OPTIONS["SCHEDULE"] must be a mapping of name -> spec') == 1


def test_pg_cron_non_mapping_entry_single_error(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-mapping schedule entry under pg_cron yields only core's E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {"nightly": "0 2 * * *"},
        },
    )
    assert out.count("Schedule 'nightly' must be a mapping.") == 1


def test_pg_cron_bad_alias_charset_rejected(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Backend alias with chars outside [A-Za-z0-9_-] rejected."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "alias": "bad.alias",
            "queues": BASE_QUEUES,
            "schedule": {
                "nightly": {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                }
            },
        },
    )
    assert "absurd.E007" in out
    assert "Backend alias contains characters other than [A-Za-z0-9_-]." in out


def test_pg_cron_bad_alias_charset_rejected_without_settings_schedule(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Alias charset validated per-backend.

    Bad alias caught even when backend has no settings SCHEDULE (admin-only).
    """
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "alias": "bad.alias",
            "queues": BASE_QUEUES,
            "schedule": {},
        },
    )
    assert "absurd.E007" in out
    assert "Backend alias contains characters other than [A-Za-z0-9_-]." in out


def test_pg_cron_missing_task_no_queue_error(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing task under pg_cron yields core's import E007 only."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {"nightly": {"cron": "0 2 * * *"}},
        },
    )
    assert "could not be imported" in out
    assert "is not declared" not in out


def test_pg_cron_unimportable_task_no_queue_error(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unimportable task under pg_cron yields core's import E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {"nightly": {"task": "tests.tasks.nope", "cron": "0 2 * * *"}},
        },
    )
    assert "could not be imported" in out
    assert "is not declared" not in out


def test_pg_cron_non_task_no_queue_error(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-task path under pg_cron yields core's not-a-task E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
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
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
    cron: t.Any,
) -> None:
    """pg_cron cron grammar DB-authoritative, structural presence is not.

    Empty or non-string cron rejected at check time (cron.schedule needs
    schedule string).
    """
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {"nightly": {"task": "tests.tasks.add", "cron": cron}},
        },
    )
    assert "absurd.E007" in out
    assert "cron must be a non-empty string." in out


def test_unknown_scheduler_value_rejected(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCHEDULER value other than 'beat'/'pg_cron' rejected."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pgcron",  # typo — missing underscore
            "queues": BASE_QUEUES,
            "schedule": {
                "nightly": {
                    "task": "tests.tasks.add",
                    "cron": "0 2 * * *",
                }
            },
        },
    )
    assert "absurd.E007" in out
    assert "unknown SCHEDULER" in out
    assert "'pgcron'" in out


def test_pg_cron_trailing_newline_name_rejected(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schedule name with trailing newline rejected (fullmatch, not match)."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
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
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
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
            "scheduler": "pg_cron",
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
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Valid 5-field cron under pg_cron passes without absurd.E007."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
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
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCHEDULE key that is integer yields E007, not TypeError."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {5: {"task": "tests.tasks.add", "cron": "0 2 * * *"}},
        },
    )
    assert "absurd.E007" in out
    assert "TypeError" not in out
