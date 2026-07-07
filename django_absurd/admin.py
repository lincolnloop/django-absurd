import contextlib
import typing as t

from django.contrib import admin, messages
from django.contrib.admin.sites import AdminSite
from django.core.paginator import Paginator
from django.db import connections
from django.db.utils import OperationalError, ProgrammingError
from django.utils.module_loading import import_string

if t.TYPE_CHECKING:
    from django.db import models as db_models

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    EntitySpec,
    build_admin_model,
    fetch_catalog_queues,
)
from django_absurd.models import Queue
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
    spec: EntitySpec | None  # None on the Queue admin (the catalog, not an entity view)
    using: str

    ordering = ("natural_key",)
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

    def has_module_permission(self, request: t.Any) -> bool:
        return True

    def get_readonly_fields(self, request: t.Any, obj: t.Any = None) -> tuple[str, ...]:
        model_fields = tuple(f.name for f in self.model._meta.get_fields())  # noqa: SLF001
        return tuple(self.readonly_fields) + model_fields

    def get_queryset(self, request: t.Any) -> t.Any:
        if self.spec is not None and not view_exists(self.spec.view_name, self.using):
            return self.model.objects.using(self.using).none()
        return self.model.objects.using(self.using).all()

    def get_object(
        self,
        request: t.Any,
        object_id: str,
        from_field: t.Any = None,
    ) -> t.Any:
        queue = object_id.split(":", 1)[0]
        queryset = self.get_queryset(request).filter(queue=queue)
        try:
            return queryset.get(pk=object_id)
        except self.model.DoesNotExist:
            return None

    def changelist_view(self, request: t.Any, extra_context: t.Any = None) -> t.Any:
        if self.spec is not None:
            try:
                stale = find_unindexed_queues(self.using)
            except (OperationalError, ProgrammingError):
                stale = []
            if stale:
                names = ", ".join(f"'{q}'" for q in stale)
                messages.warning(
                    request,
                    f"Queue(s) {names} exist but aren't indexed in the admin views "
                    "yet — run 'manage.py absurd_sync_queues' (or start a worker on "
                    "them) to include their tasks.",
                )
        return super().changelist_view(request, extra_context)


def view_exists(view_name: str, using: str) -> bool:
    with connections[using].cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", [f"absurd.{view_name}"])
        return cur.fetchone()[0] is not None


def find_unindexed_queues(using: str) -> list[str]:
    catalog = set(fetch_catalog_queues(using))
    if not catalog:
        return []
    with connections[using].cursor() as cur:
        cur.execute(
            "SELECT cl.relname "
            "FROM pg_rewrite r "
            "JOIN pg_depend d ON d.objid = r.oid "
            "JOIN pg_class cl ON cl.oid = d.refobjid "
            "JOIN pg_namespace n ON n.oid = cl.relnamespace "
            "WHERE r.ev_class = to_regclass('absurd.tasks_view') "
            "AND n.nspname = 'absurd'"
        )
        arms = {row[0][2:] for row in cur.fetchall() if row[0].startswith("t_")}
    return sorted(catalog - arms)


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


def build_queue_admin(using: str) -> type[ReadOnlyAbsurdAdmin]:
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
        run_model = build_admin_model(
            next(s for s in ADMIN_ENTITY_SPECS if s.name == "runs")
        )
        extra["inlines"] = [build_run_inline(run_model)]
        extra["fieldsets"] = TASK_FIELDSETS
        # Most recently active first: by run start, then enqueue time (both real
        # datetime columns, so the changelist shows the sort indicator and sorts on
        # click). enqueue_at is effectively unique, keeping pagination stable.
        extra["ordering"] = ("-first_started_at", "-enqueue_at")

    if spec.name == "runs":
        extra["fieldsets"] = RUN_FIELDSETS
        # Most recently active run first: by start, then creation (created_at is
        # effectively unique, keeping pagination stable).
        extra["ordering"] = ("-started_at", "-created_at")

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


TASK_FIELDSETS = (
    (None, {"fields": ("queue", "task_id", "task_name", "idempotency_key")}),
    ("State", {"fields": ("state", "attempts", "max_attempts", "last_attempt_run")}),
    ("Schedule", {"fields": ("enqueue_at", "first_started_at", "cancelled_at")}),
    (
        "Configuration",
        {"fields": ("params", "headers", "retry_strategy", "cancellation")},
    ),
    ("Result", {"fields": ("completed_payload",)}),
)

RUN_FIELDSETS = (
    (None, {"fields": ("queue", "run_id", "task", "attempt", "state")}),
    ("Claim", {"fields": ("claimed_by", "claim_expires_at", "available_at")}),
    (
        "Timing",
        {"fields": ("created_at", "started_at", "completed_at", "failed_at")},
    ),
    ("Event", {"fields": ("wake_event", "event_payload")}),
    ("Result", {"fields": ("result", "failure_reason")}),
)

RUN_INLINE_FIELDS = (
    "attempt",
    "state",
    "claimed_by",
    "started_at",
    "completed_at",
    "failed_at",
)


class ReadOnlyRunInline(admin.TabularInline):
    fk_name = "task"
    extra = 0
    can_delete = False
    show_change_link = True  # drill into the full run detail
    ordering = ("attempt",)
    fields = RUN_INLINE_FIELDS
    readonly_fields = RUN_INLINE_FIELDS

    def has_add_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False

    def has_view_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return True


def build_run_inline(run_model: type) -> type[admin.TabularInline]:
    return type("RunInline", (ReadOnlyRunInline,), {"model": run_model})


def autoregister_admin() -> None:
    backend = get_absurd_backend()
    if backend is None:
        return
    if not backend.options.get("ENABLE_ADMIN", True):
        return
    register_absurd_admin(resolve_admin_sites())


with contextlib.suppress(Exception):
    autoregister_admin()
