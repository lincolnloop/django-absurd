"""Static E007 checks for SCHEDULER="pg_cron" entries."""

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"
E007_MSG = "django-absurd: invalid SCHEDULE entry."

# tests/tasks.py declares @task(queue_name="other") and @task(queue_name="reports")
# at module level; importing any task from that module validates those queue names
# against the current backend. All tests that import from tests.tasks must
# therefore declare at least "other" and "reports" alongside "default".
BASE_QUEUES: dict = {"default": {}, "other": {}, "reports": {}}


def run_pg_cron_check(settings, capsys, options):
    """Drive check with given scheduler/queues/schedule and return captured output.

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


def test_pg_cron_task_import_raise_reports_e007_not_crash(settings, capsys):
    """A scheduled task whose module raises a non-ImportError at import must
    surface as E007, not crash `manage.py check` with a raw traceback."""
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


def test_pg_cron_six_field_cron_rejected(settings, capsys):
    """6-field cron (leading seconds) must be rejected under pg_cron."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {
                "half-minute": {
                    "task": "tests.tasks.add",
                    "cron": "*/30 * * * * *",
                }
            },
        },
    )
    assert "absurd.E007" in out
    assert "6-field cron expressions are not supported by pg_cron." in out
    assert (
        "pg_cron fires at minute granularity; use a 5-field cron expression"
        " (no leading seconds column)."
    ) in out


def test_pg_cron_bad_name_charset_rejected(settings, capsys):
    """Schedule name with spaces/special chars must be rejected under pg_cron."""
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
    assert "invalid schedule name" in out


def test_pg_cron_jobname_too_long_rejected(settings, capsys):
    """Composed jobname exceeding 63 bytes must be rejected under pg_cron."""
    # alias "default" + name long enough to push absurd:settings:default:<name> > 63 bytes
    # "absurd:settings:default:" = 24 chars, so name needs > 39 chars
    long_name = "a" * 40
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


def test_pg_cron_undeclared_task_queue_rejected(settings, capsys):
    """Task with queue_name='reports' not in declared queues must be rejected."""
    # tests.tasks.on_reports has @task(queue_name="reports"); exclude it from declared
    # queues so get_effective_queue finds it undeclared. Still include "other"
    # (required by the tasks module import) but omit "reports".
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
                    # no "queue" key — get_effective_queue falls back to task.queue_name
                }
            },
        },
    )
    assert "absurd.E007" in out
    assert "queue 'reports' is not declared" in out


def test_pg_cron_undeclared_explicit_queue_single_error(settings, capsys):
    """An undeclared explicit queue override yields exactly ONE E007 (core's)."""
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


def test_pg_cron_non_mapping_schedule_single_error(settings, capsys):
    """A non-mapping SCHEDULE under pg_cron yields only core's mapping E007."""
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


def test_pg_cron_non_mapping_entry_single_error(settings, capsys):
    """A non-mapping schedule entry under pg_cron yields only core's E007."""
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


def test_pg_cron_bad_alias_charset_rejected(settings, capsys):
    """A backend alias with characters outside [A-Za-z0-9_-] must be rejected."""
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
    assert "backend alias 'bad.alias' contains characters not allowed" in out


def test_pg_cron_missing_task_no_queue_error(settings, capsys):
    """A missing task under pg_cron yields core's import E007 only — no queue E007."""
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


def test_pg_cron_unimportable_task_no_queue_error(settings, capsys):
    """An unimportable task under pg_cron yields core's import E007 — no queue E007."""
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


def test_pg_cron_non_task_no_queue_error(settings, capsys):
    """A non-task path under pg_cron yields core's not-a-task E007 — no queue E007."""
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


def test_pg_cron_non_string_cron_rejected_cleanly(settings, capsys):
    """A non-string cron under pg_cron yields core's E007, not a TypeError."""
    out = run_pg_cron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": BASE_QUEUES,
            "schedule": {"nightly": {"task": "tests.tasks.add", "cron": 300}},
        },
    )
    assert "invalid cron expression 300." in out
    assert "is not declared" not in out


def test_unknown_scheduler_value_rejected(settings, capsys):
    """A SCHEDULER value other than 'beat'/'pg_cron' must be rejected."""
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


def test_pg_cron_trailing_newline_name_rejected(settings, capsys):
    """Schedule name with a trailing newline must be rejected (fullmatch, not match)."""
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
    assert "invalid schedule name" in out


def test_pg_cron_empty_string_queue_resolves_via_effective_queue(settings, capsys):
    """queue: "" is falsy — pg_cron resolves via task queue_name, not literal "".

    Core raises one E007 (empty string is not a declared queue name); the
    pg_cron effective-queue check must not raise a second one by treating ""
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


def test_pg_cron_valid_five_field_cron_no_error(settings, capsys):
    """Valid 5-field cron under pg_cron must pass without absurd.E007."""
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


def test_pg_cron_non_string_name_yields_e007_not_typeerror(settings, capsys):
    """SCHEDULE key that is an integer must yield E007, not a TypeError."""
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
