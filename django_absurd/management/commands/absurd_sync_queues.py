from django_absurd.management.base import AbsurdReportCommand
from django_absurd.queues import get_absurd_backends, sync_queues


class Command(AbsurdReportCommand):
    help = "Create and reconcile queues declared on each Absurd task backend."

    def handle(self, *args: object, **options: object) -> None:
        backends = get_absurd_backends()
        if not backends:
            self.stdout.write("No Absurd task backends configured.")
            return
        for alias, backend in backends.items():
            prefix = f"[{alias}] " if len(backends) > 1 else ""
            self.report_sync_result(sync_queues(backend), prefix)
