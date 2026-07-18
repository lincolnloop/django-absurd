import pytest
from django.contrib import admin as djadmin
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse_lazy

from django_absurd.admin import (
    autoregister_admin,
    register_absurd_admin,
    resolve_admin_sites,
)
from tests.core.test_admin.support import BACKEND, parse_html

pytestmark = pytest.mark.django_db(transaction=True)

LOGIN = reverse_lazy("admin:login")
INDEX = reverse_lazy("admin:index")


def test_login_page_renders(client: Client) -> None:
    assert client.get(LOGIN).status_code == 200


def test_six_entries_registered_on_default_site() -> None:
    registered = {m._meta.model_name for m in djadmin.site._registry}
    assert {"task", "run", "checkpoint", "event", "wait", "queue"} <= registered


def test_staff_user_sees_entries_in_index(client: Client, staff_user: User) -> None:
    client.force_login(staff_user)
    soup = parse_html(client.get(INDEX))
    assert soup.select_one('a[href$="/django_absurd/task/"]') is not None


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("tests.admin.custom_site",)},
        }
    }
)
def test_custom_site_registration() -> None:
    from tests.admin import custom_site  # noqa: PLC0415

    register_absurd_admin(resolve_admin_sites())
    assert any(m._meta.model_name == "task" for m in custom_site._registry)


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("nonexistent.module.site",)},
        }
    }
)
def test_bad_admin_site_fails_soft() -> None:
    assert resolve_admin_sites() == []


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {
                "ENABLE_ADMIN": False,
                "ADMIN_SITE": ("tests.admin.gated_site",),
            },
        }
    }
)
def test_admin_disabled_skips_registration() -> None:
    from tests.admin import gated_site  # noqa: PLC0415

    autoregister_admin()
    assert not any(m._meta.app_label == "django_absurd" for m in gated_site._registry)
    # flipping the switch on the same site registers — proves the gate, not emptiness
    enabled = {
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {
                "ENABLE_ADMIN": True,
                "ADMIN_SITE": ("tests.admin.gated_site",),
            },
        }
    }
    with override_settings(TASKS=enabled):
        autoregister_admin()
    assert any(m._meta.model_name == "task" for m in gated_site._registry)


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {
                "ADMIN_SITE": ("tests.admin.custom_site", "tests.admin.other_site")
            },
        }
    }
)
def test_admin_site_tuple_registers_on_all_sites() -> None:
    from tests.admin import custom_site, other_site  # noqa: PLC0415

    register_absurd_admin(resolve_admin_sites())
    assert any(m._meta.model_name == "task" for m in custom_site._registry)
    assert any(m._meta.model_name == "task" for m in other_site._registry)
