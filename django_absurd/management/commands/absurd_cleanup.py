from django.core.management.base import BaseCommand

from django_absurd.backends import get_absurd_backends
from django_absurd.cleanup import cleanup_all_queues


class Command(BaseCommand):
    help = "Delete expired task and event history per each queue's retention policy."

    def handle(self, *args: object, **options: object) -> None:
        if not get_absurd_backends():
            self.stdout.write("No Absurd task backends configured.")
            return
        for row in cleanup_all_queues():
            self.stdout.write(
                f"{row['queue_name']}: "
                f"{row['tasks_deleted']} tasks, {row['events_deleted']} events deleted"
            )
