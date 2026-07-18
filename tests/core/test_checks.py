import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.db import connection, connections

if t.TYPE_CHECKING:
    import pytest_django.fixtures

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks_setting(
    queues: dict[str, dict[str, t.Any]], database: str = "default"
) -> dict[str, dict[str, t.Any]]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": database, "QUEUES": queues},
        }
    }


def run_absurd_check(
    capsys: pytest.CaptureFixture[str],
    *args: t.Any,
    **kwargs: t.Any,
) -> str:
    try:
        call_command("check", "django_absurd", *args, **kwargs)
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_in_sync_no_warning(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    out = run_absurd_check(capsys, databases=["default"])
    assert (
        "django-absurd: declared queues are out of sync with the database." not in out
    )


def test_db_unreachable_is_silent(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"a": {}})
    real_name = settings.DATABASES["default"]["NAME"]
    settings.DATABASES["default"]["NAME"] = "absurd_nope_missing_db"
    del connections["default"]
    try:
        out = run_absurd_check(capsys, databases=["default"])
        assert (
            "django-absurd: declared queues are out of sync with the database."
            not in out
        )
    finally:
        settings.DATABASES["default"]["NAME"] = real_name
        connections["default"].close()


@pytest.mark.parametrize(
    "after",
    [
        {"synced": {}, "missing": {}},
        {"synced": {"cleanup_limit": 250}},
        {"synced": {"cleanup_ttl": "60 days"}},
    ],
    ids=["missing-queue", "mutable-scalar", "mutable-duration"],
)
def test_self_healing_drift_no_longer_warns(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
    after: dict[str, dict[str, t.Any]],
) -> None:
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting(after)
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" not in out


def test_storage_mode_drift_warns(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"q": {}})
    call_command("absurd_sync_queues")  # 'q' created unpartitioned
    settings.TASKS = build_tasks_setting({"q": {"storage_mode": "partitioned"}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" in out
    assert "storage_mode" in out
    assert "q" in out


def test_invalid_policy_modes_error(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting(
        {"q": {"storage_mode": "bogus", "detach_mode": "nope"}}
    )
    out = run_absurd_check(capsys, databases=["default"])
    assert (
        "django-absurd: invalid per-queue policy options. Queue 'q':"
        " invalid storage_mode 'bogus'." in out
    )
    assert (
        "django-absurd: invalid per-queue policy options. Queue 'q':"
        " invalid detach_mode 'nope'." in out
    )


def test_schema_absent_check_is_silent(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"a": {}})
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        out = run_absurd_check(capsys, databases=["default"])
        assert "absurd.W001" not in out
        assert "absurd.W002" not in out
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", verbosity=0)  # restore absurd schema


@pytest.mark.django_db(databases=["default", "sqlite"])
def test_check_errors_on_wrong_backend(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"x": {}}, database="sqlite")
    out = run_absurd_check(capsys)
    assert "absurd.E001" in out
    assert (
        "django-absurd requires the psycopg (v3) PostgreSQL backend. See https://www.psycopg.org/psycopg3/docs/"
        in out
    )


def test_check_errors_when_router_missing(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"x": {}}, database="absurd")
    settings.DATABASE_ROUTERS = []
    out = run_absurd_check(capsys)
    assert "absurd.E005" in out
    assert (
        "django-absurd: a non-default DATABASE is configured but "
        "AbsurdRouter is not in DATABASE_ROUTERS." in out
    )


def test_both_queue_forms_set_errors(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "QUEUES": ["a"],
            "OPTIONS": {"QUEUES": {"a": {}}},
        }
    }
    out = run_absurd_check(capsys)
    assert "absurd.E002" in out
    assert (
        "django-absurd: both top-level QUEUES and OPTIONS['QUEUES'] "
        "are set on the same backend." in out
    )


def test_pure_options_queues_no_e002(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {"default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {"a": {}}}}}
    out = run_absurd_check(capsys)
    assert "absurd.E002" not in out


def test_invalid_policy_key_errors(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"a": {"bogus_key": 1}}},
        }
    }
    out = run_absurd_check(capsys)
    assert "absurd.E002" not in out
    assert "absurd.E003" in out
    assert "a" in out


def test_invalid_storage_mode_literal_errors(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"a": {"storage_mode": "nope"}}},
        }
    }
    out = run_absurd_check(capsys)
    assert "absurd.E002" not in out
    assert "absurd.E003" in out


def test_single_absurd_backend_no_e004(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"q": {}})
    assert "more than one Absurd backend" not in run_absurd_check(
        capsys, databases=["default"]
    )


def test_two_absurd_backends_distinct_db_error(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": "default", "QUEUES": {"a": {}}},
        },
        "other": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": "absurd", "QUEUES": {"b": {}}},
        },
    }
    out = run_absurd_check(capsys)
    assert "absurd.E004" in out
    assert "django-absurd: more than one Absurd backend is configured." in out
    assert (
        "django-absurd uses a single Absurd backend per project"
        " — configure exactly one AbsurdBackend in TASKS." in out
    )


def test_two_absurd_backends_same_db_error(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    # https://github.com/lincolnloop/django-absurd/issues/63
    settings.TASKS = {
        "a": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {}}},
        "b": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {}}},
    }
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.E004" in out
    assert "django-absurd: more than one Absurd backend is configured." in out
    assert (
        "django-absurd uses a single Absurd backend per project"
        " — configure exactly one AbsurdBackend in TASKS." in out
    )


def test_plain_check_skips_db_state(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"synced": {}, "missing": {}})
    out = run_absurd_check(capsys)  # plain `check`, no --database
    assert "absurd.W002" not in out


def test_check_with_database_runs_db_state(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"synced": {}, "missing": {}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" not in out


E009_MSG = "django-absurd: OPTIONS['DEFAULT_MAX_ATTEMPTS'] must be an integer >= 1."


@pytest.mark.parametrize("value", [-1, 0, 1.5, "3", True])
def test_default_max_attempts_invalid_is_error(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
    value: float | str | bool,
) -> None:
    # A DEFAULT_MAX_ATTEMPTS < 1 (or a non-int) would feed 0/garbage into every
    # reconciled schedule's max_attempts and crash migrate against the CheckConstraint;
    # catch it at check time. bool is rejected (int subclass, not an attempt count).
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"default": {}}, "DEFAULT_MAX_ATTEMPTS": value},
        }
    }
    out = run_absurd_check(capsys)
    assert E009_MSG in out
    assert "absurd.E009" in out


def test_default_max_attempts_valid_no_error(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"default": {}}, "DEFAULT_MAX_ATTEMPTS": 3},
        }
    }
    out = run_absurd_check(capsys)
    assert "absurd.E009" not in out


E010_MSG = "django-absurd: invalid CLEANUP option."
E010_HINT = (
    "Set CLEANUP to a dict with a single 'schedule' key:"
    ' OPTIONS["CLEANUP"] = {"schedule": "<cron>"}.'
)


@pytest.mark.parametrize(
    ("cleanup", "scheduler"),
    [
        ("0 2 * * *", "beat"),
        ({"schedule": ""}, "beat"),
        ({"schedule": ""}, "pg_cron"),
        ({"schedule": "0 2 * * *", "unknown": 1}, "beat"),
        ({"schedule": "not a cron"}, "beat"),
        ({"schedule": 5}, "beat"),
    ],
)
def test_invalid_cleanup_errors(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
    cleanup: str | dict[str, t.Any],
    scheduler: str,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULER": scheduler,
                "CLEANUP": cleanup,
            },
        }
    }
    out = run_absurd_check(capsys)
    assert E010_MSG in out
    assert E010_HINT in out
    assert "absurd.E010" in out


@pytest.mark.parametrize("scheduler", ["beat", "pg_cron"])
def test_valid_cleanup_no_error(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
    scheduler: str,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULER": scheduler,
                "CLEANUP": {"schedule": "0 2 * * *"},
            },
        }
    }
    out = run_absurd_check(capsys)
    assert "absurd.E010" not in out
