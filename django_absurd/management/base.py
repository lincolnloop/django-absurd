from django.core.management.base import BaseCommand

from django_absurd.queues import SyncResult


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
