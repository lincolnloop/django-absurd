import pytest
from bs4 import BeautifulSoup
from django.contrib import admin as djadmin
from django.contrib.auth.models import Permission
from django.urls import reverse_lazy

from django_absurd.admin import resolve_admin_sites
from django_absurd.pg_cron.admin import (
    autoregister_scheduled_task_admin,
    register_scheduled_task_admin,
)
from django_absurd.pg_cron.models import ScheduledTask
from tests.admin import custom_site, gated_site, other_site

pytestmark = pytest.mark.django_db(transaction=True)

INDEX = reverse_lazy("admin:index")
BACKEND = "django_absurd.backends.AbsurdBackend"


def test_scheduledtask_registered_on_default_site():
    registered = {m._meta.model_name for m in djadmin.site._registry}
    assert "scheduledtask" in registered


def test_staff_user_with_view_permission_sees_scheduledtask_in_index(
    client, staff_user
):
    staff_user.user_permissions.add(
        Permission.objects.get(
            codename="view_scheduledtask",
            content_type__app_label="django_absurd_pg_cron",
        )
    )
    client.force_login(staff_user)
    soup = BeautifulSoup(client.get(INDEX).content, "html.parser")
    assert (
        soup.select_one('a[href$="/django_absurd_pg_cron/scheduledtask/"]') is not None
    )


def test_custom_admin_site_registration(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {"ADMIN_SITE": ("tests.admin.custom_site",)},
        }
    }
    register_scheduled_task_admin(resolve_admin_sites())
    assert custom_site.is_registered(ScheduledTask)


def test_autoregister_skips_when_enable_admin_false(settings):
    gated_site._registry.pop(ScheduledTask, None)
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {
                "ENABLE_ADMIN": False,
                "ADMIN_SITE": ("tests.admin.gated_site",),
            },
        }
    }
    autoregister_scheduled_task_admin()
    assert not gated_site.is_registered(ScheduledTask)


def test_register_is_idempotent(settings):
    other_site._registry.pop(ScheduledTask, None)
    register_scheduled_task_admin([other_site])
    register_scheduled_task_admin(
        [other_site]
    )  # second call skips (already registered)
    assert other_site.is_registered(ScheduledTask)


def test_autoregister_skips_when_no_absurd_backend(settings):
    gated_site._registry.pop(ScheduledTask, None)
    settings.TASKS = {}
    autoregister_scheduled_task_admin()  # backend is None -> no-op, no raise
    assert not gated_site.is_registered(ScheduledTask)


def test_autoregister_registers_when_enable_admin_true(settings):
    other_site._registry.pop(ScheduledTask, None)
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {
                "ENABLE_ADMIN": True,
                "ADMIN_SITE": ("tests.admin.other_site",),
            },
        }
    }
    autoregister_scheduled_task_admin()
    assert other_site.is_registered(ScheduledTask)
