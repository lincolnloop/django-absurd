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


def run_pgcron_check(settings, capsys, options):
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


def test_pgcron_six_field_cron_rejected(settings, capsys):
    """6-field cron (leading seconds) must be rejected under pg_cron."""
    out = run_pgcron_check(
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
    assert "minute-granularity" in out
    assert "beat scheduler" in out


def test_pgcron_bad_name_charset_rejected(settings, capsys):
    """Schedule name with spaces/special chars must be rejected under pg_cron."""
    out = run_pgcron_check(
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


def test_pgcron_jobname_too_long_rejected(settings, capsys):
    """Composed jobname exceeding 63 bytes must be rejected under pg_cron."""
    # alias "default" + name long enough to push absurd:settings:default:<name> > 63 bytes
    # "absurd:settings:default:" = 24 chars, so name needs > 39 chars
    long_name = "a" * 40
    out = run_pgcron_check(
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


def test_pgcron_undeclared_task_queue_rejected(settings, capsys):
    """Task with queue_name='reports' not in declared queues must be rejected."""
    # tests.tasks.on_reports has @task(queue_name="reports"); exclude it from declared
    # queues so effective_queue finds it undeclared. Still include "other" (required by
    # tasks module import) but omit "reports".
    out = run_pgcron_check(
        settings,
        capsys,
        {
            "scheduler": "pg_cron",
            "queues": {"default": {}, "other": {}},
            "schedule": {
                "ghostly": {
                    "task": "tests.tasks.on_reports",
                    "cron": "0 2 * * *",
                    # no explicit "queue" key — effective_queue falls back to task.queue_name
                }
            },
        },
    )
    assert "absurd.E007" in out
    assert "queue 'reports' is not declared" in out


def test_unknown_scheduler_value_rejected(settings, capsys):
    """A SCHEDULER value other than 'beat'/'pg_cron' must be rejected."""
    out = run_pgcron_check(
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


def test_pgcron_trailing_newline_name_rejected(settings, capsys):
    """Schedule name with a trailing newline must be rejected (fullmatch, not match)."""
    out = run_pgcron_check(
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


def test_pgcron_valid_five_field_cron_no_error(settings, capsys):
    """Valid 5-field cron under pg_cron must pass without absurd.E007."""
    out = run_pgcron_check(
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
