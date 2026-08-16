import typing as t

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.exceptions import BackendNotConfiguredError, DjangoAbsurdError
from django_absurd.queues import SyncResult

BEAT_DISABLED_UNDER_PG_CRON = (
    "the pg_cron app is installed: schedules run in the database via pg_cron,"
    " so the beat process is disabled."
    " Reconcile the pg_cron jobs with 'manage.py absurd_sync_crons'"
    " (migrate does it too)."
)


def resolve_backend() -> AbsurdBackend:
    backends = get_absurd_backends()
    if len(backends) == 1:
        return next(iter(backends.values()))
    raise BackendNotConfiguredError(len(backends))


class AbsurdCommand(BaseCommand):
    """Base for every ``absurd_*`` command: translates configuration failures into
    a clean ``CommandError`` instead of a raw traceback.

    Overrides ``execute()`` rather than ``handle()``: no command needs to rename its
    own ``handle``, ``call_command`` and ``run_from_argv`` both route through
    ``execute()`` so both get the same contract, and ``--traceback`` still prints the
    original chain via ``from exc``.
    """

    def execute(self, *args: t.Any, **options: t.Any) -> t.Any:
        try:
            return super().execute(*args, **options)
        except (ImproperlyConfigured, DjangoAbsurdError) as exc:
            raise CommandError(str(exc)) from exc


class AbsurdReportCommand(AbsurdCommand):
    """Base for commands that report a queue SyncResult to stdout/stderr."""

    def report_sync_result(
        self,
        result: SyncResult,
        stdout_prefix: str = "",
        stderr_prefix: str = "",
        empty_message: str | None = None,
    ) -> None:
        if result.created:
            self.stdout.write(f"{stdout_prefix}Created: {', '.join(result.created)}")
        if result.reconciled:
            self.stdout.write(
                f"{stdout_prefix}Reconciled: {', '.join(result.reconciled)}"
            )
        if empty_message and not result.created and not result.reconciled:
            self.stdout.write(f"{stdout_prefix}{empty_message}")
        for warning in result.storage_warnings:
            self.stderr.write(self.style.WARNING(f"{stderr_prefix}{warning}"))
