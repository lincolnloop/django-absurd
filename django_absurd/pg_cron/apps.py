import logging
import typing as t

from django.apps import AppConfig, apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.color import color_style
from django.db import connections
from django.db.models.signals import (
    post_delete,
    post_migrate,
    post_save,
    pre_save,
)
from django.db.utils import InternalError, OperationalError, ProgrammingError
from django.utils.connection import ConnectionDoesNotExist

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron import signals

if t.TYPE_CHECKING:
    from django_absurd.backends import AbsurdBackend

logger = logging.getLogger("django_absurd")

ORIGINAL_DATABASE_NAMES: dict[str, str] = {}


class PgCronConfig(AppConfig):
    name = "django_absurd.pg_cron"
    label = "django_absurd_pg_cron"
    verbose_name = "Absurd Cron"

    def ready(self) -> None:
        for db_alias, db_config in settings.DATABASES.items():
            # str(...): django-stubs' plugin types DATABASES[alias]["NAME"] as
            # Collection[str], not str (a TypedDict-inference quirk) — it's always a
            # plain string (or sqlite's Path) at runtime.
            ORIGINAL_DATABASE_NAMES.setdefault(db_alias, str(db_config["NAME"]))

        # Side-effect import: running the module registers its @register'd E007 checks.
        import django_absurd.pg_cron.checks  # noqa: F401, PLC0415

        scheduled_task = apps.get_model("django_absurd_pg_cron", "ScheduledTask")

        # Reconcile pg_cron jobs as part of `migrate`. This app's post_migrate
        # signal fires after core django_absurd's (INSTALLED_APPS order), so the
        # queue tables the scheduled jobs target already exist when this runs.
        post_migrate.connect(reconcile_crons_after_migrate, sender=self)

        # Reject a write forced onto a non-absurd database before the row is inserted.
        pre_save.connect(
            signals.reject_cross_database_save,
            sender=scheduled_task,
            dispatch_uid="django_absurd_pg_cron.reject_cross_database_save",
        )

        # Every ScheduledTask write (settings reconcile, admin authoring, direct ORM)
        # (un)schedules its pg_cron job through one central path.
        post_save.connect(
            signals.schedule_job_on_save,
            sender=scheduled_task,
            dispatch_uid="django_absurd_pg_cron.schedule_job_on_save",
        )
        post_delete.connect(
            signals.unschedule_job_on_delete,
            sender=scheduled_task,
            dispatch_uid="django_absurd_pg_cron.unschedule_job_on_delete",
        )


def reconcile_crons_after_migrate(
    sender: AppConfig,
    *,
    verbosity: int = 1,
    stdout: t.TextIO | None = None,
    **kwargs: object,
) -> None:
    from django_absurd.pg_cron.reconcile import (  # noqa: PLC0415
        sync_admin_crons,
        sync_crons,
    )

    style = color_style()
    absurd_backends = get_absurd_backends()
    if not absurd_backends:
        return
    alias, backend = next(iter(absurd_backends.items()))
    try:
        if not resolve_sync_schedules_option(backend):
            return
        created, pruned = sync_crons(backend)
        sync_admin_crons()
        lines = []
        if created:
            lines.append(f"  Scheduled {created}")
        if pruned:
            lines.append(f"  Pruned {pruned}")
        if lines and verbosity >= 1 and stdout is not None:
            stdout.write(
                style.MIGRATE_HEADING(f"Reconciling pg_cron schedules ({alias}):")
            )
            for line in lines:
                stdout.write(line)
    except (
        ConnectionDoesNotExist,
        ImproperlyConfigured,
        OperationalError,
        ProgrammingError,
        InternalError,
        ImportError,
        TypeError,
        KeyError,
        AttributeError,
        ValueError,
    ):
        # Best-effort: migrate must never break. Skip this backend on an
        # unreachable DB, a misconfigured OPTIONS["DATABASE"] alias, tables not
        # yet present (faked/adopted migration, or a multi-DB migrate firing
        # post_migrate before the Absurd DB is migrated), a bad dotted path in a
        # schedule, a malformed SCHEDULE spec, or an unserializable arg.
        logger.warning(
            "django-absurd: skipped cron reconcile for backend %r",
            alias,
            exc_info=True,
        )


def resolve_sync_schedules_option(backend: "AbsurdBackend") -> bool:
    live_name = str(connections[backend.database].settings_dict["NAME"])
    is_test_db = live_name != ORIGINAL_DATABASE_NAMES.get(backend.database)
    if is_test_db:
        return bool(backend.options.get("SYNC_SCHEDULES_ON_TEST_DB", False))
    return bool(backend.options.get("SYNC_SCHEDULES_ON_MIGRATE", True))
