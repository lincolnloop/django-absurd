import dataclasses
import typing as t

import psycopg.sql
from django.apps.registry import Apps
from django.db import connections, models, transaction
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.exceptions import (
    ADMIN_VIEW_READONLY_MSG,
    QueueReadOnlyError,
    ViewNotProvisionedError,
)

PRIVATE_ADMIN_APPS = Apps()


@dataclasses.dataclass(frozen=True)
class EntitySpec:
    name: str
    prefix: str
    view_name: str
    model_name: str
    verbose: str
    natural_key_sql: psycopg.sql.Composable
    columns: tuple[tuple[str, str], ...]
    has_state: bool
    has_status: bool
    list_display: tuple[str, ...]
    search_fields: tuple[str, ...]


ADMIN_ENTITY_SPECS: tuple[EntitySpec, ...] = (
    EntitySpec(
        name="tasks",
        prefix="t",
        view_name="tasks_view",
        model_name="Task",
        verbose="task",
        natural_key_sql=psycopg.sql.SQL("task_id::text"),
        columns=(
            ("task_id", "uuid"),
            ("task_name", "text"),
            ("params", "jsonb"),
            ("headers", "jsonb"),
            ("retry_strategy", "jsonb"),
            ("max_attempts", "int"),
            ("cancellation", "jsonb"),
            ("enqueue_at", "timestamptz"),
            ("first_started_at", "timestamptz"),
            ("state", "text"),
            ("attempts", "int"),
            ("last_attempt_run", "uuid"),
            ("completed_payload", "jsonb"),
            ("cancelled_at", "timestamptz"),
            ("idempotency_key", "text"),
        ),
        has_state=True,
        has_status=False,
        list_display=(
            "natural_key",
            "queue",
            "task_name",
            "state",
            "attempts",
            "enqueue_at",
            "first_started_at",
        ),
        search_fields=("task_id", "task_name"),
    ),
    EntitySpec(
        name="runs",
        prefix="r",
        view_name="runs_view",
        model_name="Run",
        verbose="run",
        natural_key_sql=psycopg.sql.SQL("run_id::text"),
        columns=(
            ("run_id", "uuid"),
            ("task_id", "uuid"),
            ("attempt", "int"),
            ("state", "text"),
            ("claimed_by", "text"),
            ("claim_expires_at", "timestamptz"),
            ("available_at", "timestamptz"),
            ("wake_event", "text"),
            ("event_payload", "jsonb"),
            ("started_at", "timestamptz"),
            ("completed_at", "timestamptz"),
            ("failed_at", "timestamptz"),
            ("result", "jsonb"),
            ("failure_reason", "jsonb"),
            ("created_at", "timestamptz"),
        ),
        has_state=True,
        has_status=False,
        list_display=("natural_key", "queue", "task_id", "attempt", "state"),
        search_fields=("run_id", "task__task_id", "claimed_by"),
    ),
    EntitySpec(
        name="checkpoints",
        prefix="c",
        view_name="checkpoints_view",
        model_name="Checkpoint",
        verbose="checkpoint",
        natural_key_sql=psycopg.sql.SQL("task_id::text || ':' || checkpoint_name"),
        columns=(
            ("task_id", "uuid"),
            ("checkpoint_name", "text"),
            ("state", "jsonb"),
            ("status", "text"),
            ("owner_run_id", "uuid"),
            ("updated_at", "timestamptz"),
        ),
        has_state=False,
        has_status=True,
        list_display=("natural_key", "queue", "task_id", "checkpoint_name", "status"),
        search_fields=("task_id", "checkpoint_name"),
    ),
    EntitySpec(
        name="events",
        prefix="e",
        view_name="events_view",
        model_name="Event",
        verbose="event",
        natural_key_sql=psycopg.sql.SQL("event_name::text"),
        columns=(
            ("event_name", "text"),
            ("payload", "jsonb"),
            ("emitted_at", "timestamptz"),
        ),
        has_state=False,
        has_status=False,
        list_display=("natural_key", "queue", "event_name", "emitted_at"),
        search_fields=("event_name",),
    ),
    EntitySpec(
        name="waits",
        prefix="w",
        view_name="waits_view",
        model_name="Wait",
        verbose="wait",
        natural_key_sql=psycopg.sql.SQL("run_id::text || ':' || step_name"),
        columns=(
            ("task_id", "uuid"),
            ("run_id", "uuid"),
            ("step_name", "text"),
            ("event_name", "text"),
            ("timeout_at", "timestamptz"),
            ("created_at", "timestamptz"),
        ),
        has_state=False,
        has_status=False,
        list_display=("natural_key", "queue", "task_id", "run_id", "step_name"),
        search_fields=("task_id", "run_id", "step_name"),
    ),
)


VIEW_NOT_PROVISIONED_MSG = (
    "Absurd union view not provisioned. Run 'manage.py absurd_sync_queues'."
)


class AbsurdViewQuerySet(models.QuerySet):
    def _fetch_all(self) -> None:
        try:
            super()._fetch_all()
        except (ProgrammingError, OperationalError) as exc:
            raise ViewNotProvisionedError(VIEW_NOT_PROVISIONED_MSG) from exc

    def count(self) -> int:
        return translate_view_errors(super().count)()

    def exists(self) -> bool:
        return translate_view_errors(super().exists)()

    def aggregate(self, *args: t.Any, **kwargs: t.Any) -> dict[str, t.Any]:
        return translate_view_errors(super().aggregate)(*args, **kwargs)


def translate_view_errors(
    fn: t.Callable[..., t.Any],
) -> t.Callable[..., t.Any]:
    def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
        try:
            return fn(*args, **kwargs)
        except (ProgrammingError, OperationalError) as exc:
            raise ViewNotProvisionedError(VIEW_NOT_PROVISIONED_MSG) from exc

    return wrapper


AbsurdViewManager = models.Manager.from_queryset(AbsurdViewQuerySet)


def build_admin_model(spec: EntitySpec) -> type[models.Model]:
    existing = PRIVATE_ADMIN_APPS.all_models["django_absurd"].get(
        spec.model_name.lower()
    )
    if existing is not None:
        return existing

    fields: dict[str, t.Any] = {
        "natural_key": models.TextField(primary_key=True),
        "queue": models.TextField(),
    }
    for col_name, col_type in spec.columns:
        field_name, field = build_model_field(spec, col_name, col_type)
        fields[field_name] = field

    def save(self: models.Model, *args: object, **kwargs: object) -> t.NoReturn:
        raise QueueReadOnlyError(ADMIN_VIEW_READONLY_MSG)

    def delete(self: models.Model, *args: object, **kwargs: object) -> t.NoReturn:
        raise QueueReadOnlyError(ADMIN_VIEW_READONLY_MSG)

    def model_str(self: t.Any) -> str:
        return self.natural_key

    fields["save"] = save
    fields["delete"] = delete
    fields["__str__"] = model_str
    fields["objects"] = AbsurdViewManager()

    fields["Meta"] = type(
        "Meta",
        (),
        {
            "managed": False,
            "app_label": "django_absurd",
            "db_table": psycopg.sql.Identifier("absurd", spec.view_name).as_string(
                None
            ),
            "apps": PRIVATE_ADMIN_APPS,
            "verbose_name": spec.verbose,
            "verbose_name_plural": f"{spec.verbose}s",
        },
    )
    fields["__module__"] = __name__

    return type(spec.model_name, (models.Model,), fields)


def build_model_field(
    spec: EntitySpec, col_name: str, col_type: str
) -> tuple[str, models.Field]:
    # Tasks' task_id is the FK target for the Run inline, so it must be unique.
    if spec.name == "tasks" and col_name == "task_id":
        return "task_id", models.UUIDField(null=True, unique=True)
    # Runs join to their task on task_id — model it as a (constraint-free) FK named
    # `task` so the admin can inline runs under a task. The attname stays `task_id`.
    if spec.name == "runs" and col_name == "task_id":
        tasks_spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
        return "task", models.ForeignKey(
            build_admin_model(tasks_spec),
            to_field="task_id",
            db_column="task_id",
            db_constraint=False,
            on_delete=models.DO_NOTHING,
            null=True,
            related_name="runs",
        )
    return col_name, make_field(col_type)


def build_queue_table_model(spec: EntitySpec, queue: str) -> type[models.Model]:
    sanitized_queue = queue.replace("-", "_")
    model_name = f"QueueTable_{spec.prefix}_{sanitized_queue}"
    existing = PRIVATE_ADMIN_APPS.all_models["django_absurd"].get(model_name.lower())
    if existing is not None:
        return existing

    pk_col_name = spec.columns[0][0]
    fields: dict[str, t.Any] = {}
    for col_name, col_type in spec.columns:
        if col_name == pk_col_name:
            field_cls = FIELD_TYPE_MAP[col_type]
            fields[col_name] = field_cls(primary_key=True)
        else:
            fields[col_name] = make_field(col_type)

    def save(self: models.Model, *args: object, **kwargs: object) -> t.NoReturn:
        raise QueueReadOnlyError(ADMIN_VIEW_READONLY_MSG)

    def delete(self: models.Model, *args: object, **kwargs: object) -> t.NoReturn:
        raise QueueReadOnlyError(ADMIN_VIEW_READONLY_MSG)

    fields["save"] = save
    fields["delete"] = delete

    fields["Meta"] = type(
        "Meta",
        (),
        {
            "managed": False,
            "app_label": "django_absurd",
            "db_table": psycopg.sql.Identifier(
                "absurd", f"{spec.prefix}_{queue}"
            ).as_string(None),
            "apps": PRIVATE_ADMIN_APPS,
        },
    )
    fields["__module__"] = __name__

    return type(model_name, (models.Model,), fields)


FIELD_TYPE_MAP: dict[str, type[models.Field]] = {
    "uuid": models.UUIDField,
    "text": models.TextField,
    "int": models.IntegerField,
    "jsonb": models.JSONField,
    "timestamptz": models.DateTimeField,
}


def make_field(col_type: str) -> models.Field:
    field_cls = FIELD_TYPE_MAP[col_type]
    return field_cls(null=True)


def fetch_catalog_queues(using: str) -> list[str]:
    with connections[using].cursor() as cur:
        cur.execute("SELECT queue_name FROM absurd.queues ORDER BY queue_name")
        return [row[0] for row in cur.fetchall()]


def build_union_view_sql(spec: EntitySpec, queues: list[str]) -> str:
    view = psycopg.sql.Identifier("absurd", spec.view_name)
    drop = psycopg.sql.SQL("DROP VIEW IF EXISTS {view};").format(view=view)
    if not queues:
        body = compose_empty_arm(spec)
    else:
        arms = [compose_queue_arm(spec, q) for q in queues]
        body = psycopg.sql.SQL(" UNION ALL ").join(arms)
    create = psycopg.sql.SQL("CREATE VIEW {view} AS {body}").format(
        view=view, body=body
    )
    return (
        psycopg.sql.SQL("{drop}\n{create}")
        .format(drop=drop, create=create)
        .as_string(None)
    )


def rebuild_admin_view(spec: EntitySpec, queues: list[str], using: str) -> None:
    sql = build_union_view_sql(spec, queues)
    with transaction.atomic(using=using), connections[using].cursor() as cur:
        cur.execute(sql)


def rebuild_views(using: str) -> None:
    queues = fetch_catalog_queues(using)
    for spec in ADMIN_ENTITY_SPECS:
        rebuild_admin_view(spec, queues, using)


SQL_TYPE_NULLS: dict[str, psycopg.sql.SQL] = {
    "uuid": psycopg.sql.SQL("NULL::uuid"),
    "text": psycopg.sql.SQL("NULL::text"),
    "int": psycopg.sql.SQL("NULL::int"),
    "jsonb": psycopg.sql.SQL("NULL::jsonb"),
    "timestamptz": psycopg.sql.SQL("NULL::timestamptz"),
}


def compose_queue_arm(spec: EntitySpec, queue: str) -> psycopg.sql.Composable:
    table = psycopg.sql.Identifier("absurd", f"{spec.prefix}_{queue}")
    queue_lit = psycopg.sql.Literal(queue)
    pk_expr = (
        psycopg.sql.SQL("{q}::text || ':' || ").format(q=queue_lit)
        + spec.natural_key_sql
    )
    col_list = psycopg.sql.SQL(", ").join(
        psycopg.sql.Identifier(col) for col, _ in spec.columns
    )
    return psycopg.sql.SQL(
        "SELECT {q}::text AS queue, {pk} AS natural_key, {cols} FROM {tbl}"
    ).format(q=queue_lit, pk=pk_expr, cols=col_list, tbl=table)


def compose_empty_arm(spec: EntitySpec) -> psycopg.sql.Composable:
    null_parts = psycopg.sql.SQL(", ").join(
        SQL_TYPE_NULLS[col_type]
        + psycopg.sql.SQL(" AS ")
        + psycopg.sql.Identifier(col_name)
        for col_name, col_type in spec.columns
    )
    return psycopg.sql.SQL(
        "SELECT NULL::text AS queue, NULL::text AS natural_key, {nulls} WHERE false"
    ).format(nulls=null_parts)
