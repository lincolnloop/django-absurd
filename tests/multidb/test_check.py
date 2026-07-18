import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(databases=["default", "absurd"])

ABSURD = "django_absurd.backends.AbsurdBackend"


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


def test_storage_mode_drift_detected_on_non_default_alias(
    settings: SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
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
    out = run_absurd_check(capsys, databases=["absurd"])
    assert "absurd.W002" in out
    assert (
        "django-absurd: a queue's declared storage_mode differs from the database"
        " (storage_mode is immutable)." in out
    )
    assert "Affected: d" in out
