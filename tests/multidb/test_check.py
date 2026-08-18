import pytest
from django.core.management import call_command
from pytest_django import Settings

pytestmark = pytest.mark.django_db(databases=["default", "absurd"])

ABSURD = "django_absurd.backends.AbsurdBackend"


def test_storage_mode_drift_detected_on_non_default_alias(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    # W002 now fires only on storage_mode drift (immutable; never self-heals). This
    # locks the alias threading through query_queue_state on a non-default database.
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": "absurd", "QUEUES": {"d": {}}},
        }
    }
    call_command("absurd_sync_queues")  # 'd' created unpartitioned on alias 'absurd'
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "DATABASE": "absurd",
                "QUEUES": {"d": {"storage_mode": "partitioned"}},
            },
        }
    }
    call_command("check", "django_absurd", databases=["absurd"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "absurd.W002" in out
    assert (
        "django-absurd: a queue's declared storage_mode differs from the database"
        " (storage_mode is immutable)." in out
    )
    assert "Affected: d" in out
