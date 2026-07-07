"""Read-only Django admin for pg_cron ScheduledTask rows (registered at import)."""

import contextlib
import typing as t

from django_absurd.admin import ReadOnlyAdminBase, resolve_admin_sites
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.queues import get_absurd_backend


class ScheduledTaskAdmin(ReadOnlyAdminBase):
    ordering = ("alias", "name")
    list_display = (
        "name",
        "alias",
        "task",
        "queue",
        "cron",
        "enabled",
        "source",
        "updated_at",
    )
    list_filter = ("alias", "enabled", "source", "queue")
    search_fields = ("name", "task")
    fieldsets = (
        ("Identity", {"fields": ("source", "alias", "name")}),
        ("Schedule", {"fields": ("task", "queue", "cron", "enabled")}),
        (
            "Spawn options",
            {
                "fields": (
                    "args",
                    "kwargs",
                    "max_attempts",
                    "retry_strategy",
                    "headers",
                    "cancellation",
                    "idempotency_key",
                )
            },
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )


def register_scheduled_task_admin(sites: t.Iterable[t.Any]) -> None:
    for site in sites:
        if not site.is_registered(ScheduledTask):
            site.register(ScheduledTask, ScheduledTaskAdmin)


def autoregister_scheduled_task_admin() -> None:
    backend = get_absurd_backend()
    if backend is None:
        return
    if not backend.options.get("ENABLE_ADMIN", True):
        return
    register_scheduled_task_admin(resolve_admin_sites())


with contextlib.suppress(Exception):
    autoregister_scheduled_task_admin()
