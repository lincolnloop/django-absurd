import logging

from django.apps import AppConfig
from django.db.models.signals import post_migrate

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


def reconcile_crons_after_migrate(sender: AppConfig, **kwargs: object) -> None:
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415
    from django.db.utils import (  # noqa: PLC0415
        InternalError,
        OperationalError,
        ProgrammingError,
    )

    from django_absurd.backends import get_absurd_backends  # noqa: PLC0415
    from django_absurd.pg_cron.reconcile import (  # noqa: PLC0415
        sync_crons,
        teardown_crons,
    )

    for alias, backend in get_absurd_backends().items():
        try:
            if backend.scheduler == "pg_cron":
                sync_crons(backend)
            else:
                teardown_crons(backend)
        except ProgrammingError:
            # Expected quiet no-op: the pg_cron extension isn't installed on the
            # target DB, or the schema isn't there (faked/adopted migration).
            logger.debug(
                "django-absurd: skipped cron reconcile for backend %r"
                " (pg_cron or schema absent)",
                alias,
            )
            continue
        except (
            ImproperlyConfigured,
            OperationalError,
            InternalError,
            ImportError,
            TypeError,
            KeyError,
        ):
            # Best-effort: migrate must never break. Skip this backend on an
            # unreachable DB, a bad dotted path in a schedule, a malformed
            # SCHEDULE spec, an unserializable arg, or a pre-1.4 pg_cron.
            logger.warning(
                "django-absurd: skipped cron reconcile for backend %r",
                alias,
                exc_info=True,
            )
            continue
