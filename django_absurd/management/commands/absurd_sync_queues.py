from django.core.management.base import BaseCommand

from django_absurd.queues import (
    SyncResult,
    get_absurd_backends,
    sync_queues,
    write_sync_report,
)


class Command(BaseCommand):
    help = "Create and reconcile queues declared on each Absurd task backend."

    def handle(self, *args: object, **options: object) -> None:
        backends = get_absurd_backends()
        if not backends:
            self.stdout.write("No Absurd task backends configured.")
            return
        for alias, backend in backends.items():
            prefix = f"[{alias}] " if len(backends) > 1 else ""
            self.report_result(prefix, sync_queues(backend))

    def report_result(self, prefix: str, result: SyncResult) -> None:
        write_sync_report(self, result, prefix)
