"""Writable Django admin for pg_cron ScheduledTask rows (registered at import).

The admin lane (``source="admin"``) is fully writable; settings-declared rows
(``source="settings"``, owned by reconcile) stay read-only, gated per object.
"""

import contextlib
import typing as t

from django import forms
from django.contrib import admin
from django.contrib.admin.options import IS_POPUP_VAR
from django.contrib.admin.utils import flatten_fieldsets
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from django_absurd.admin import resolve_admin_sites
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import build_scheduled_fields
from django_absurd.queues import get_absurd_backend
from django_absurd.validators import validate_task_path

if t.TYPE_CHECKING:
    from django.contrib.admin.options import _FieldsetSpec
    from django.contrib.admin.sites import AdminSite
    from django.http import HttpRequest, HttpResponse
    from django.utils.functional import _StrOrPromise

    _ScheduledTaskFormBase = forms.ModelForm[ScheduledTask]
    _ScheduledTaskAdminBase = admin.ModelAdmin[ScheduledTask]
else:
    _ScheduledTaskFormBase = forms.ModelForm
    _ScheduledTaskAdminBase = admin.ModelAdmin


class ScheduledTaskForm(_ScheduledTaskFormBase):
    class Meta:
        model = ScheduledTask
        # source is admin-owned and read-only (set on the instance, never in the form);
        # its column type/choices/help live on the model.
        fields = (
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

    def validate_unique(self) -> None:
        try:
            self.instance.validate_unique()
        except ValidationError as exc:
            self.add_error(None, exc)

    # A blank args/kwargs textarea cleans to None (JSONField's empty value), which would
    # hit the NOT NULL columns as an IntegrityError (HTTP 500). Fall back to the field
    # defaults instead so an empty box means "no positional args" / "no kwargs".
    def clean_args(self) -> list[t.Any]:
        value = self.cleaned_data.get("args")
        return [] if value is None else value

    def clean_kwargs(self) -> dict[str, t.Any]:
        value = self.cleaned_data.get("kwargs")
        return {} if value is None else value


type FormErrorValueOrSequence = (
    ValidationError | _StrOrPromise | t.Sequence[ValidationError | _StrOrPromise]
)


class ScheduledTaskCreateForm(ScheduledTaskForm):
    class Meta(ScheduledTaskForm.Meta):
        # The create step collects only identity + cron; every spawn column is
        # resolved from the task's decorators in clean(), and the row is created
        # disabled so the user reviews it on the change page before activating.
        fields = ("name", "task", "cron")  # type: ignore[assignment]

    def clean(self) -> dict[str, t.Any]:
        # Resolve every spawn column from the task's decorators and create the row
        # disabled for review on the change page. validate_task_path normally runs in
        # Model.full_clean, but that keys errors to the model field and would need the
        # spawn columns already set; instead validate the path explicitly here first so
        # an unimportable / not-a-task path is reported on the task field, and
        # build_scheduled_fields — which imports and binds the task — is only reached
        # once the path is known-good, so it cannot raise. Columns set on self.instance
        # here survive _post_clean's construct_instance (which writes only form fields)
        # into instance.full_clean, where Model.clean validates the resolved queue.
        # (A resolved-but-undeclared queue's model error is keyed to the queue field,
        # which add_error re-homes onto the form.)
        cleaned = super().clean() or {}
        self.instance.enabled = False
        backend = get_absurd_backend()
        if "task" in cleaned and backend is not None:
            task_path = cleaned["task"]
            try:
                validate_task_path(task_path)
            except ValidationError as exc:
                self.add_error("task", exc)
            else:
                for field, value in build_scheduled_fields(backend, task_path).items():
                    setattr(self.instance, field, value)
        return cleaned

    @t.overload
    def add_error(
        self, field: None, error: t.Mapping[str, FormErrorValueOrSequence]
    ) -> None: ...
    @t.overload
    def add_error(self, field: str | None, error: FormErrorValueOrSequence) -> None: ...
    def add_error(
        self,
        field: str | None,
        error: t.Mapping[str, FormErrorValueOrSequence] | FormErrorValueOrSequence,
    ) -> None:
        # The resolved spawn columns (queue, retry_kind, ...) aren't fields on this
        # 4-field create form, so a model-validation error keyed to one of them (e.g. a
        # resolved queue that isn't declared for the backend) would raise "has no field
        # named ..." (HTTP 500). Re-home any error for a field this form doesn't expose
        # onto NON_FIELD_ERRORS so it renders as a form error instead.
        if isinstance(error, ValidationError) and hasattr(error, "error_dict"):
            rehomed: dict[str, list[ValidationError]] = {}
            for name, messages in error.error_dict.items():
                key = name if name in self.fields else NON_FIELD_ERRORS
                rehomed.setdefault(key, []).extend(messages)
            error = ValidationError(rehomed)
        if isinstance(error, t.Mapping):
            # error carries per-field errors for multiple fields; BaseForm.add_error's
            # own contract requires field=None here — pass the literal, not field
            # (a caller passing both a field name and a Mapping error violates that
            # contract; letting Django's own add_error raise on it isn't our job here).
            super().add_error(None, error)
        else:
            super().add_error(field, error)


class ScheduledTaskAdmin(_ScheduledTaskAdminBase):
    form = ScheduledTaskForm
    add_fieldsets: "_FieldsetSpec" = ((None, {"fields": ("name", "task", "cron")}),)
    save_on_top = True
    ordering = ("name",)
    list_display = (
        "name",
        "task",
        "queue",
        "cron",
        "enabled",
        "source",
        "updated_at",
    )
    list_filter = ("enabled", "source", "queue")
    search_fields = ("name", "task")
    fieldsets = (
        # Activation up top: the review step's one action is flipping a resolved,
        # disabled schedule on.
        ("Activation", {"fields": ("enabled",)}),
        ("Identity", {"fields": ("source", "name")}),
        ("Schedule", {"fields": ("task", "queue", "cron")}),
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
        self,
        request: "HttpRequest",
        obj: ScheduledTask | None = None,
        change: bool = False,
        **kwargs: t.Any,
    ) -> "type[forms.ModelForm[ScheduledTask]]":
        if obj is None:
            kwargs["form"] = ScheduledTaskCreateForm
        return super().get_form(request, obj, change=change, **kwargs)

    def get_fieldsets(
        self, request: "HttpRequest", obj: ScheduledTask | None = None
    ) -> "_FieldsetSpec":
        if obj is None:
            return self.add_fieldsets
        return super().get_fieldsets(request, obj)

    def response_add(
        self,
        request: "HttpRequest",
        obj: ScheduledTask,
        post_url_continue: str | None = None,
    ) -> "HttpResponse":
        # Land on the change page so the user reviews the resolved (disabled) row and
        # activates it, rather than dropping back to the changelist — except when
        # "Save and add another" was pressed or we're in a popup.
        if "_addanother" not in request.POST and IS_POPUP_VAR not in request.POST:
            request.POST = request.POST.copy()  # type: ignore[assignment]  # QueryDict.copy() returns mutable QueryDict; HttpRequest.POST is typed as _ImmutableQueryDict
            request.POST["_continue"] = 1  # type: ignore[misc, assignment]  # QueryDict accepts int values at runtime
        return super().response_add(request, obj, post_url_continue)

    def has_change_permission(
        self, request: "HttpRequest", obj: ScheduledTask | None = None
    ) -> bool:
        # settings-declared rows are read-only regardless of Django permissions;
        # admin rows require the usual change permission.
        return super().has_change_permission(request, obj) and (
            obj is None or obj.source == ScheduledTask.Source.ADMIN
        )

    def has_delete_permission(
        self, request: "HttpRequest", obj: ScheduledTask | None = None
    ) -> bool:
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.source == ScheduledTask.Source.ADMIN
        )

    def get_readonly_fields(
        self, request: "HttpRequest", obj: ScheduledTask | None = None
    ) -> tuple[str, ...]:
        if obj is not None and obj.source == ScheduledTask.Source.SETTINGS:
            return tuple(flatten_fieldsets(self.get_fieldsets(request, obj)))
        if obj is not None:
            return ("created_at", "name", "source", "updated_at")
        return ("created_at", "source", "updated_at")


def register_scheduled_task_admin(sites: t.Iterable["AdminSite"]) -> None:
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
