import contextlib
import typing as t

from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.core.paginator import Paginator
from django.db.utils import OperationalError, ProgrammingError
from django.urls import reverse
from django.utils.html import format_html
from django.utils.module_loading import import_string

if t.TYPE_CHECKING:
    from django.db import models as db_models

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    EntitySpec,
    build_admin_model,
    ensure_view_current,
    fetch_catalog_queues,
    invalidate_view_cache,
)
from django_absurd.queues import get_absurd_backend, resolve_absurd_database

ADMIN_COUNT_CAP = 1000


class BoundedCountPaginator(Paginator):
    @property
    def count(self) -> int:
        qs = t.cast(
            "db_models.QuerySet[t.Any]", self.object_list[: ADMIN_COUNT_CAP + 1]
        )
        return min(qs.count(), ADMIN_COUNT_CAP)


class AbsurdQueueListFilter(admin.SimpleListFilter):
    parameter_name = "queue"
    title = "queue"

    def __init__(
        self,
        request: t.Any,
        params: t.Any,
        model: t.Any,
        model_admin: "ReadOnlyAbsurdAdmin",
    ) -> None:
        self.using: str = getattr(model_admin, "using", resolve_absurd_database())
        super().__init__(request, params, model, model_admin)

    def lookups(self, request: t.Any, model_admin: t.Any) -> list[tuple[str, str]]:
        try:
            queues = fetch_catalog_queues(self.using)
        except (OperationalError, ProgrammingError):
            return []
        return [(q, q) for q in queues]

    def queryset(self, request: t.Any, queryset: t.Any) -> t.Any:
        value = self.value()
        if value:
            return queryset.filter(queue=value)
        return queryset


class ReadOnlyAbsurdAdmin(admin.ModelAdmin):
    spec: EntitySpec
    using: str

    ordering = ("admin_pk",)
    show_full_result_count = False
    paginator = BoundedCountPaginator

    def has_add_permission(self, request: t.Any) -> bool:
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False

    def has_view_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return True

    def has_module_perms(self, request: t.Any) -> bool:
        return True

    def has_module_permission(self, request: t.Any) -> bool:
        return True

    def get_readonly_fields(self, request: t.Any, obj: t.Any = None) -> tuple[str, ...]:
        model_fields = tuple(f.name for f in self.model._meta.get_fields())  # noqa: SLF001
        return tuple(self.readonly_fields) + model_fields

    def get_queryset(self, request: t.Any) -> t.Any:
        try:
            ensure_view_current(self.spec, self.using)
            qs = self.model.objects.using(self.using).all()
            qs.exists()
        except (ProgrammingError, OperationalError):
            pass
        else:
            return qs
        try:
            invalidate_view_cache(self.spec.view_name)
            ensure_view_current(self.spec, self.using)
            qs = self.model.objects.using(self.using).all()
            qs.exists()
        except (ProgrammingError, OperationalError):
            return self.model.objects.none()
        else:
            return qs

    def get_object(
        self,
        request: t.Any,
        object_id: str,
        from_field: t.Any = None,
    ) -> t.Any:
        queue = object_id.split(":", 1)[0]
        queryset = self.get_queryset(request).filter(queue=queue)
        model = self.model
        field = (
            model._meta.pk  # noqa: SLF001
            if from_field is None
            else model._meta.get_field(from_field)  # noqa: SLF001
        )
        try:
            return queryset.get(**{field.name: object_id})
        except model.DoesNotExist:
            return None


def resolve_admin_sites() -> list[AdminSite]:
    backend = get_absurd_backend()
    if backend is not None:
        paths = backend.options.get("ADMIN_SITE", ("django.contrib.admin.site",))
    else:
        paths = ("django.contrib.admin.site",)

    sites: list[AdminSite] = []
    for path in paths:
        try:
            obj = import_string(path)
        except ImportError:
            continue
        if not isinstance(obj, AdminSite):
            continue
        sites.append(obj)
    return sites


def register_absurd_admin(sites: t.Iterable[AdminSite]) -> None:
    from django_absurd.models import Queue  # noqa: PLC0415

    backend = get_absurd_backend()
    using = backend.database if backend is not None else resolve_absurd_database()

    for site in sites:
        for spec in ADMIN_ENTITY_SPECS:
            model = build_admin_model(spec)
            if site.is_registered(model):
                continue
            entity_admin = build_entity_admin(spec, model, using)
            site.register(model, entity_admin)

        if not site.is_registered(Queue):
            site.register(Queue, build_queue_admin(using))


@admin.display(description="runs")
def runs_link(self: t.Any, obj: t.Any) -> str:
    url = reverse("admin:django_absurd_absurdrun_changelist")
    return format_html('<a href="{}?q={}">runs</a>', url, obj.task_id)


def build_queue_admin(using: str) -> type[ReadOnlyAbsurdAdmin]:
    def get_queryset(self: t.Any, request: t.Any) -> t.Any:
        return self.model.objects.using(self.using).all()

    def get_object(
        self: t.Any,
        request: t.Any,
        object_id: str,
        from_field: t.Any = None,
    ) -> t.Any:
        try:
            return self.get_queryset(request).get(pk=object_id)
        except self.model.DoesNotExist:
            return None

    return type(
        "QueueAdmin",
        (ReadOnlyAbsurdAdmin,),
        {
            "spec": None,
            "using": using,
            "ordering": ("queue_name",),
            "list_display": ("queue_name", "created_at", "storage_mode"),
            "list_filter": [],
            "search_fields": ("queue_name",),
            "readonly_fields": (),
            "get_queryset": get_queryset,
            "get_object": get_object,
        },
    )


def build_entity_admin(
    spec: EntitySpec, model: type, using: str
) -> type[ReadOnlyAbsurdAdmin]:
    list_filter: list[t.Any] = [AbsurdQueueListFilter]
    if spec.has_state:
        list_filter.append("state")
    if spec.has_status:
        list_filter.append("status")

    list_display = spec.list_display
    search_fields = spec.search_fields
    readonly_fields: tuple[str, ...] = ()

    extra: dict[str, t.Any] = {}
    if spec.name == "tasks":
        readonly_fields = ("runs_link",)
        extra["runs_link"] = runs_link

    if spec.name == "runs":
        search_fields = (*spec.search_fields, "task_id")

    return type(
        f"{spec.model_name}Admin",
        (ReadOnlyAbsurdAdmin,),
        {
            "spec": spec,
            "using": using,
            "list_display": list_display,
            "list_filter": list_filter,
            "search_fields": search_fields,
            "readonly_fields": readonly_fields,
            **extra,
        },
    )


def autoregister_admin() -> None:
    backend = get_absurd_backend()
    if backend is None:
        return
    if not backend.options.get("ENABLE_ADMIN", True):
        return
    register_absurd_admin(resolve_admin_sites())


with contextlib.suppress(Exception):
    autoregister_admin()
