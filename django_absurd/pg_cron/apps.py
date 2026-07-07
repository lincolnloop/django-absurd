import logging
import typing as t

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.core.management.color import color_style
from django.db.models.signals import post_migrate
from django.db.utils import InternalError, OperationalError, ProgrammingError

from django_absurd.backends import get_absurd_backends

logger = logging.getLogger("django_absurd")


class PgCronConfig(AppConfig):
    name = "django_absurd.pg_cron"
    label = "django_absurd_pg_cron"
    verbose_name = "Absurd pg_cron"

    def ready(self) -> None:
        import django_absurd.pg_cron.checks  # noqa: F401, PLC0415

        # Reconcile pg_cron jobs as part of `migrate`. This app's post_migrate
        # signal fires after core django_absurd's (INSTALLED_APPS order), so the
        # queue tables the scheduled jobs target already exist when this runs.
        post_migrate.connect(reconcile_crons_after_migrate, sender=self)


def reconcile_crons_after_migrate(
    sender: AppConfig,
    *,
    verbosity: int = 1,
    stdout: t.Any = None,
    **kwargs: object,
) -> None:
    from django_absurd.pg_cron.reconcile import (  # noqa: PLC0415
        sync_crons,
        teardown_crons,
    )

    style = color_style()
    for alias, backend in get_absurd_backends().items():
        try:
            if backend.scheduler == "pg_cron":
                created, pruned = sync_crons(backend)
                lines = []
                if created:
                    lines.append(f"  Scheduled {created}")
                if pruned:
                    lines.append(f"  Pruned {pruned}")
                if lines and verbosity >= 1 and stdout is not None:
                    stdout.write(
                        style.MIGRATE_HEADING(
                            f"Reconciling pg_cron schedules ({alias}):"
                        )
                    )
                    for line in lines:
                        stdout.write(line)
            else:
                removed = teardown_crons(backend)
                if removed > 0 and verbosity >= 1 and stdout is not None:
                    stdout.write(
                        f"  Removed {removed} pg_cron schedule(s)"
                        f' — backend {alias!r} no longer uses SCHEDULER="pg_cron"'
                    )
        except (
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
            # unreachable DB, tables not yet present (faked/adopted migration, or
            # a multi-DB migrate firing post_migrate before the Absurd DB is
            # migrated), a bad dotted path in a schedule, a malformed SCHEDULE
            # spec, or an unserializable arg.
            logger.warning(
                "django-absurd: skipped cron reconcile for backend %r",
                alias,
                exc_info=True,
            )
            continue
