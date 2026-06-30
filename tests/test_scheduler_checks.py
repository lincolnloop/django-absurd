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
        f"{E007_MSG} Schedule 'x': task 'tests.tasks.nope' could not be imported."
    ) in out
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
