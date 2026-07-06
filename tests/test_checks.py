import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.db import connection, connections

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks_setting(queues, database="default"):
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": database, "QUEUES": queues},
        }
    }


def run_absurd_check(capsys, *args, **kwargs):
    try:
        call_command("check", "django_absurd", *args, **kwargs)
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_in_sync_no_warning(settings, capsys):
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    out = run_absurd_check(capsys, databases=["default"])
    assert (
        "django-absurd: declared queues are out of sync with the database." not in out
    )


def test_db_unreachable_is_silent(settings, capsys):
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
def test_self_healing_drift_no_longer_warns(settings, capsys, after):
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting(after)
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" not in out


def test_storage_mode_drift_warns(settings, capsys):
    settings.TASKS = build_tasks_setting({"q": {}})
    call_command("absurd_sync_queues")  # 'q' created unpartitioned
    settings.TASKS = build_tasks_setting({"q": {"storage_mode": "partitioned"}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" in out
    assert "storage_mode" in out
    assert "q" in out


def test_schema_absent_check_is_silent(settings, capsys):
    settings.TASKS = build_tasks_setting({"a": {}})
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        out = run_absurd_check(capsys, databases=["default"])
        assert "absurd.W001" not in out
        assert "absurd.W002" not in out
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", verbosity=0)  # restore core AND the pg_cron app


@pytest.mark.django_db(databases=["default", "sqlite"])
def test_check_errors_on_wrong_backend(settings, capsys):
    settings.TASKS = build_tasks_setting({"x": {}}, database="sqlite")
    out = run_absurd_check(capsys)
    assert "absurd.E001" in out
    assert (
        "django-absurd requires the psycopg (v3) PostgreSQL backend. See https://www.psycopg.org/psycopg3/docs/"
        in out
    )


def test_check_errors_when_router_missing(settings, capsys):
    settings.TASKS = build_tasks_setting({"x": {}}, database="absurd")
    settings.DATABASE_ROUTERS = []
    out = run_absurd_check(capsys)
    assert "absurd.E005" in out
    assert (
        "django-absurd: a non-default DATABASE is configured but AbsurdRouter is not in DATABASE_ROUTERS."
        in out
    )


def test_both_queue_forms_set_errors(settings, capsys):
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
        "django-absurd: both top-level QUEUES and OPTIONS['QUEUES'] are set on the same backend."
        in out
    )


def test_pure_options_queues_no_e002(settings, capsys):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {"a": {}}}}}
    out = run_absurd_check(capsys)
    assert "absurd.E002" not in out


def test_invalid_policy_key_errors(settings, capsys):
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


def test_invalid_storage_mode_literal_errors(settings, capsys):
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"a": {"storage_mode": "nope"}}},
        }
    }
    out = run_absurd_check(capsys)
    assert "absurd.E002" not in out
    assert "absurd.E003" in out


def test_multiple_backends_distinct_db_errors(settings, capsys):
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


def test_plain_check_skips_db_state(settings, capsys):
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"synced": {}, "missing": {}})
    out = run_absurd_check(capsys)  # plain `check`, no --database
    assert "absurd.W002" not in out


def test_check_with_database_runs_db_state(settings, capsys):
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"synced": {}, "missing": {}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" not in out
