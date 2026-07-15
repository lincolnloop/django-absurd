"""Writable Django admin for pg_cron ScheduledTask rows (registered at import).

The admin lane (``source="admin"``) is fully writable; settings-declared rows
(``source="settings"``, owned by reconcile) stay read-only, gated per object.
"""

import contextlib
import typing as t

from django import forms
from django.contrib import admin
from django.contrib.admin.utils import flatten_fieldsets
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.http import HttpResponseRedirect
from django.tasks.exceptions import InvalidTask
from django.urls import reverse

from django_absurd.admin import resolve_admin_sites
from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask, get_declared_queue_choices
from django_absurd.pg_cron.reconcile import build_scheduled_fields
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


class ScheduledTaskCreateForm(ScheduledTaskForm):
    class Meta(ScheduledTaskForm.Meta):
        # The create step collects only identity + cron; every spawn column is
        # resolved from the task's decorators in _post_clean, and the row is created
        # disabled so the user reviews it on the change page before activating.
        fields = ("alias", "name", "task", "cron")  # type: ignore[assignment]

    def _post_clean(self) -> None:
        # Resolve every spawn column from the task's decorators and create the row
        # disabled for review on the change page. Resolving imports/binds the task to
        # the backend; a task whose own queue isn't declared there raises rather than
        # returning, so route that to the task field instead of HTTP 500 (mirrors
        # validate_task_path, which reports an unimportable task on the task field).
        if "alias" in self.cleaned_data and "task" in self.cleaned_data:
            backend = get_absurd_backends().get(self.cleaned_data["alias"])
            if backend is not None:
                try:
                    fields = build_scheduled_fields(backend, self.cleaned_data["task"])
                except InvalidTask as exc:
                    self.add_error("task", str(exc))
                else:
                    for field, value in fields.items():
                        setattr(self.instance, field, value)
            self.instance.enabled = False
        super()._post_clean()  # type: ignore[misc]

    def add_error(self, field: t.Any, error: t.Any) -> None:
        # The resolved spawn columns (queue, retry_kind, ...) aren't fields on this
        # 4-field create form, so a model-validation error keyed to one of them (e.g. a
        # resolved queue that isn't declared for the backend) would raise "has no field
        # named ..." (HTTP 500). Re-home any error for a field this form doesn't expose
        # onto NON_FIELD_ERRORS so it renders as a form error instead.
        if isinstance(error, ValidationError) and hasattr(error, "error_dict"):
            rehomed: dict[str, t.Any] = {}
            for name, messages in error.error_dict.items():
                key = name if name in self.fields else NON_FIELD_ERRORS
                rehomed.setdefault(key, []).extend(messages)
            error = ValidationError(rehomed)
        super().add_error(field, error)


class ScheduledTaskAdmin(admin.ModelAdmin):
    form = ScheduledTaskForm
    add_fieldsets = ((None, {"fields": ("alias", "name", "task", "cron")}),)
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

    def get_form(
        self, request: t.Any, obj: t.Any = None, change: bool = False, **kwargs: t.Any
    ) -> t.Any:
        if obj is None:
            kwargs["form"] = ScheduledTaskCreateForm
        return super().get_form(request, obj, change=change, **kwargs)

    def get_fieldsets(self, request: t.Any, obj: t.Any = None) -> t.Any:
        if obj is None:
            return self.add_fieldsets
        return super().get_fieldsets(request, obj)

    def response_add(
        self, request: t.Any, obj: t.Any, post_url_continue: t.Any = None
    ) -> t.Any:
        # Land on the change page so the user reviews the resolved (disabled) row and
        # activates it, rather than dropping back to the changelist.
        return HttpResponseRedirect(
            reverse("admin:django_absurd_pg_cron_scheduledtask_change", args=[obj.pk])
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
