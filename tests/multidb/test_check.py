import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

pytestmark = pytest.mark.django_db(databases=["default", "absurd"])

ABSURD = "django_absurd.backends.AbsurdBackend"


def run_absurd_check(capsys, *args, **kwargs):
    try:
        call_command("check", "django_absurd", *args, **kwargs)
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_duration_drift_detected_on_non_default_alias(settings, capsys):
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "DATABASE": "absurd",
                "QUEUES": {"d": {"cleanup_ttl": "90 days"}},
            },
        }
    }
    call_command("absurd_sync_queues")
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "DATABASE": "absurd",
                "QUEUES": {"d": {"cleanup_ttl": "30 days"}},
            },
        }
    }
    out = run_absurd_check(capsys, databases=["absurd"])
    assert "absurd.W002" in out
