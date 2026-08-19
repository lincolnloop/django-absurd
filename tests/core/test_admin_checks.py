import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from pytest_django import Settings

BACKEND = "django_absurd.backends.AbsurdBackend"
IMMEDIATE = "django.tasks.backends.immediate.ImmediateBackend"

BAD_PATH = "nonexistent.module.site"


def run_check(capsys: pytest.CaptureFixture[str]) -> str:
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_bad_admin_site_path_emits_e006(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": (BAD_PATH,)},
        }
    }
    out = run_check(capsys)
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E006) django-absurd: OPTIONS['ADMIN_SITE'] entry"
        " 'nonexistent.module.site' could not be imported.\n"
        "\tHINT: Set ADMIN_SITE to a tuple of dotted paths to AdminSite"
        " instances.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_non_bool_enable_admin_emits_e006(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ENABLE_ADMIN": "yes"},
        }
    }
    out = run_check(capsys)
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E006) django-absurd: OPTIONS['ENABLE_ADMIN'] must be a"
        " bool.\n"
        "\tHINT: Set ENABLE_ADMIN to True or False.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_valid_admin_config_no_e006(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("django.contrib.admin.site",)},
        }
    }
    out = run_check(capsys)
    assert out == "System check identified no issues (0 silenced).\n"


def test_no_absurd_backend_emits_no_e006(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {"default": {"BACKEND": IMMEDIATE}}
    out = run_check(capsys)
    assert out == "System check identified no issues (0 silenced).\n"


def test_admin_site_not_a_sequence_emits_e006(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": "django.contrib.admin.site"},
        }
    }
    out = run_check(capsys)
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E006) django-absurd: OPTIONS['ADMIN_SITE'] must be a tuple"
        " or list of dotted-path strings.\n"
        "\tHINT: Set ADMIN_SITE to a tuple of dotted paths to AdminSite"
        " instances.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_admin_site_not_an_adminsite_emits_e006(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("decimal.Decimal",)},
        }
    }
    out = run_check(capsys)
    assert out == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E006) django-absurd: OPTIONS['ADMIN_SITE'] entry"
        " 'decimal.Decimal' is not an AdminSite instance.\n"
        "\tHINT: Set ADMIN_SITE to a tuple of dotted paths to AdminSite"
        " instances.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )
