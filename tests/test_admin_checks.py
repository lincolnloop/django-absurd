from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import override_settings

from django_absurd.checks import (
    E006_ADMIN_SITE_HINT,
    E006_ADMIN_SITE_TYPE_MSG,
    E006_ENABLE_ADMIN_HINT,
    E006_ENABLE_ADMIN_MSG,
)

BACKEND = "django_absurd.backends.AbsurdBackend"
IMMEDIATE = "django.tasks.backends.immediate.ImmediateBackend"

BAD_PATH = "nonexistent.module.site"
E006_BAD_PATH_MSG = (
    f"django-absurd: OPTIONS['ADMIN_SITE'] entry {BAD_PATH!r} could not be imported."
)


def run_check(capsys):
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": (BAD_PATH,)},
        }
    }
)
def test_bad_admin_site_path_emits_e006(capsys):
    out = run_check(capsys)
    assert "absurd.E006" in out
    assert E006_BAD_PATH_MSG in out
    assert E006_ADMIN_SITE_HINT in out


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ENABLE_ADMIN": "yes"},
        }
    }
)
def test_non_bool_enable_admin_emits_e006(capsys):
    out = run_check(capsys)
    assert "absurd.E006" in out
    assert E006_ENABLE_ADMIN_MSG in out
    assert E006_ENABLE_ADMIN_HINT in out


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("django.contrib.admin.site",)},
        }
    }
)
def test_valid_admin_config_no_e006(capsys):
    out = run_check(capsys)
    assert "absurd.E006" not in out
    assert "admin.E0" not in out
    assert "System check identified no issues" in out


@override_settings(TASKS={"default": {"BACKEND": IMMEDIATE}})
def test_no_absurd_backend_emits_no_e006(capsys):
    out = run_check(capsys)
    assert "absurd.E006" not in out


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": "django.contrib.admin.site"},
        }
    }
)
def test_admin_site_not_a_sequence_emits_e006(capsys):
    out = run_check(capsys)
    assert "absurd.E006" in out
    assert E006_ADMIN_SITE_TYPE_MSG in out


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("decimal.Decimal",)},
        }
    }
)
def test_admin_site_not_an_adminsite_emits_e006(capsys):
    out = run_check(capsys)
    assert "absurd.E006" in out
    assert "is not an AdminSite instance" in out
