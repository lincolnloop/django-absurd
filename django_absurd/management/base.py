from django.core.management.base import BaseCommand, CommandError

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.queues import SyncResult

BEAT_DISABLED_UNDER_PG_CRON = (
    "SCHEDULER is 'pg_cron': schedules run in the database via pg_cron,"
    " so the beat process is disabled."
    " Reconcile the pg_cron jobs with 'manage.py absurd_sync_crons'"
    " (migrate does it too)."
)


def resolve_backend() -> AbsurdBackend:
    backends = get_absurd_backends()
    if len(backends) == 1:
        return next(iter(backends.values()))
    if len(backends) == 0:
        msg = "No Absurd backend configured."
        raise CommandError(msg)
    aliases = ", ".join(sorted(backends))
    msg = f"Multiple Absurd backends found: {aliases}. Use --alias to select one."
    raise CommandError(msg)


class AbsurdReportCommand(BaseCommand):
    """Base for commands that report a queue SyncResult to stdout/stderr."""

    def report_sync_result(
        self, result: SyncResult, prefix: str = "", empty_message: str | None = None
    ) -> None:
        if result.created:
            self.stdout.write(f"{prefix}Created: {', '.join(result.created)}")
        if result.reconciled:
            self.stdout.write(f"{prefix}Reconciled: {', '.join(result.reconciled)}")
        if empty_message and not result.created and not result.reconciled:
            self.stdout.write(f"{prefix}{empty_message}")
        for warning in result.storage_warnings:
            self.stderr.write(self.style.WARNING(f"{prefix}{warning}"))
