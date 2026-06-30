import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"

E007_MSG = "django-absurd: invalid SCHEDULE entry."


@pytest.fixture
def run_check(capsys, settings):
    def _run(schedule):
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


def test_valid_schedule_no_error(run_check):
    out = run_check({"ok": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    assert "absurd.E007" not in out


def test_unimportable_task(run_check):
    out = run_check({"x": {"task": "tests.tasks.nope", "cron": "0 2 * * *"}})
    assert (
        f"{E007_MSG} Schedule 'x': task 'tests.tasks.nope' could not be imported"
    ) in out
    assert "absurd.E007" in out


def test_non_import_error_at_task_import(run_check):
    out = run_check(
        {"x": {"task": "tests.raises_on_import.anything", "cron": "0 2 * * *"}}
    )
    assert (
        f"{E007_MSG} Schedule 'x': task 'tests.raises_on_import.anything' could not be imported"
    ) in out
    assert "RuntimeError" in out
    assert "boom at import" in out
    assert "absurd.E007" in out


def test_schedule_not_a_mapping(run_check):
    out = run_check(["nightly"])
    assert 'OPTIONS["SCHEDULE"] must be a mapping of name -> spec' in out
    assert "absurd.E007" in out


def test_schedule_entry_not_a_mapping(run_check):
    out = run_check({"nightly": "0 2 * * *"})
    assert f"{E007_MSG} Schedule 'nightly' must be a mapping." in out
    assert "absurd.E007" in out


def test_not_a_task(run_check):
    out = run_check({"x": {"task": "tests.tasks.Payload", "cron": "0 2 * * *"}})
    assert (
        f"{E007_MSG} Schedule 'x': 'tests.tasks.Payload' is not a Django task."
    ) in out
    assert "absurd.E007" in out


def test_bad_cron(run_check):
    out = run_check({"x": {"task": "tests.tasks.add", "cron": "not-cron"}})
    assert (f"{E007_MSG} Schedule 'x': invalid cron expression 'not-cron'.") in out
    assert "absurd.E007" in out


def test_non_string_cron(run_check):
    # A non-string cron (e.g. forgot the quotes) must yield a clean E007, not an
    # AttributeError from croniter.is_valid — the check runs at worker/beat boot.
    out = run_check({"x": {"task": "tests.tasks.add", "cron": 300}})
    assert (f"{E007_MSG} Schedule 'x': invalid cron expression 300.") in out
    assert "absurd.E007" in out


def test_unknown_key(run_check):
    out = run_check({"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "bogus": 1}})
    assert (f"{E007_MSG} Schedule 'x': unknown key 'bogus'.") in out
    assert "absurd.E007" in out


def test_non_serializable_args(run_check):
    out = run_check(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "args": [object()]}}
    )
    assert (f"{E007_MSG} Schedule 'x': args is not JSON-serializable.") in out
    assert "absurd.E007" in out


def test_undeclared_queue(run_check):
    out = run_check(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "queue": "ghost"}}
    )
    assert (f"{E007_MSG} Schedule 'x': queue 'ghost' is not declared.") in out
    assert "absurd.E007" in out


def test_non_string_queue(run_check):
    # A non-string queue (e.g. a list) must yield a clean E007, not a TypeError
    # from the `queue not in declared_queues` membership test.
    out = run_check(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "queue": ["bad"]}}
    )
    assert (f"{E007_MSG} Schedule 'x': queue ['bad'] is not declared.") in out
    assert "absurd.E007" in out
