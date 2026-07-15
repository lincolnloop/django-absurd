"""Writable Django admin for pg_cron ScheduledTask rows (registered at import).

The admin lane (``source="admin"``) is fully writable; settings-declared rows
(``source="settings"``, owned by reconcile) stay read-only, gated per object.
"""

import contextlib
import typing as t

from django import forms
from django.contrib import admin
from django.contrib.admin.utils import flatten_fieldsets
from django.core.exceptions import ValidationError

from django_absurd.admin import resolve_admin_sites
from django_absurd.pg_cron.models import ScheduledTask, get_declared_queue_choices
from django_absurd.queues import get_absurd_backend


class ScheduledTaskForm(forms.ModelForm):
    class Meta:
        model = ScheduledTask
        # source is admin-owned and read-only (set on the instance, never in the form);
        # its column type/choices/help live on the model.
        fields = (
            "alias",
            "name",
            "task",
            "queue",
            "cron",
            "enabled",
            "args",
            "kwargs",
            "max_attempts",
            "retry_kind",
            "retry_base_seconds",
            "retry_factor",
            "retry_max_seconds",
            "headers",
            "cancellation_max_duration",
            "cancellation_max_delay",
            "idempotency_key",
        )

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            self.instance.source = ScheduledTask.Source.ADMIN
        queue_field = self.fields.get("queue")
        if isinstance(queue_field, forms.ChoiceField) and self.instance.queue:
            # A stored queue that's no longer declared has dropped out of the field's
            # (declared-queues) choices; add it back so the change form renders the real
            # value instead of silently resubmitting a different one. It stays invalid —
            # clean() rejects an undeclared queue with a validation error.
            declared = get_declared_queue_choices()
            if self.instance.queue not in {value for value, _ in declared}:
                queue_field.choices = [
                    *declared,
                    (self.instance.queue, self.instance.queue),
                ]

    def validate_unique(self) -> None:
        # source is read-only, so Django excludes it and would skip the
        # (source, alias, name) unique check — a duplicate would then surface as an
        # IntegrityError (HTTP 500). Un-exclude it so Django's own check runs and a
        # duplicate is a form error; self.instance.source is already pinned to ADMIN.
        # _get_validation_exclusions / _update_errors are real BaseModelForm methods
        # missing from django-stubs.
        exclude = self._get_validation_exclusions()  # type: ignore[attr-defined]
        exclude.discard("source")
        try:
            self.instance.validate_unique(exclude=exclude)
        except ValidationError as exc:
            self._update_errors(exc)  # type: ignore[attr-defined]

    # A blank args/kwargs textarea cleans to None (JSONField's empty value), which would
    # hit the NOT NULL columns as an IntegrityError (HTTP 500). Fall back to the field
    # defaults instead so an empty box means "no positional args" / "no kwargs".
    def clean_args(self) -> t.Any:
        value = self.cleaned_data.get("args")
        return [] if value is None else value

    def clean_kwargs(self) -> t.Any:
        value = self.cleaned_data.get("kwargs")
        return {} if value is None else value


class ScheduledTaskAdmin(admin.ModelAdmin):
    form = ScheduledTaskForm
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
            "Retry",
            {
                "fields": (
                    "max_attempts",
                    "retry_kind",
                    "retry_base_seconds",
                    "retry_factor",
                    "retry_max_seconds",
                )
            },
        ),
        (
            "Cancellation",
            {"fields": ("cancellation_max_duration", "cancellation_max_delay")},
        ),
        (
            "Spawn options",
            {"fields": ("args", "kwargs", "headers", "idempotency_key")},
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        # settings-declared rows are read-only regardless of Django permissions;
        # admin rows require the usual change permission.
        return super().has_change_permission(request, obj) and (
            obj is None or obj.source == ScheduledTask.Source.ADMIN
        )

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.source == ScheduledTask.Source.ADMIN
        )

    def get_readonly_fields(self, request: t.Any, obj: t.Any = None) -> tuple[str, ...]:
        if obj is not None and obj.source == ScheduledTask.Source.SETTINGS:
            return tuple(flatten_fieldsets(self.get_fieldsets(request, obj)))
        if obj is not None:
            return ("alias", "created_at", "name", "source", "updated_at")
        return ("created_at", "source", "updated_at")


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
