import typing as t

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.core.management.color import color_style
from django.db.models.signals import post_migrate
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.admin_views import PRIVATE_ADMIN_APPS
from django_absurd.backends import get_absurd_backends
from django_absurd.exceptions import SchemaNotInstalledError
from django_absurd.queues import provision_backend


class AbsurdConfig(AppConfig):
    name = "django_absurd"
    label = "django_absurd"
    verbose_name = "Absurd"

    def ready(self) -> None:
        # Side-effect import: registers the @register'd checks. In-function
        # because checks imports models and ready() runs before models load
        # (AppRegistryNotReady).
        import django_absurd.checks  # noqa: F401, PLC0415

        # The synthesized admin models live in PRIVATE_ADMIN_APPS, so their
        # _meta.app_config resolves there. Point it at this config so the admin
        # change-view breadcrumb shows the app's verbose_name instead of blank.
        PRIVATE_ADMIN_APPS.app_configs.setdefault("django_absurd", self)

        # Provision declared queues + their admin views as part of `migrate`.
        post_migrate.connect(provision_queues_after_migrate, sender=self)


def provision_queues_after_migrate(
    sender: AppConfig,
    *,
    verbosity: int = 1,
    stdout: t.IO[str] | None = None,
    **kwargs: object,
) -> None:
    style = color_style()
    for alias, backend in get_absurd_backends().items():
        try:
            result = provision_backend(backend)
        except (
            ImproperlyConfigured,
            OperationalError,
            ProgrammingError,
            SchemaNotInstalledError,
        ):
            # Best-effort: skip when the schema isn't installed on the target DB
            # (e.g. a faked/adopted migration, or a non-Absurd database). This is
            # a signal receiver, not a command — it never runs through the
            # command base, so this except tuple is permanent, not a stopgap.
            continue
        lines = [f"  Created {name!r}" for name in result.created]
        lines += [f"  Reconciled {name!r}" for name in result.reconciled]
        lines += [style.WARNING(f"  {w}") for w in result.storage_warnings]
        if lines and verbosity >= 1 and stdout is not None:
            heading = f"Provisioning Absurd queues ({alias}):"
            stdout.write(style.MIGRATE_HEADING(heading))
            for line in lines:
                stdout.write(line)
