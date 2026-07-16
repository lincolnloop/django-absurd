from django.core.management.base import BaseCommand

from django_absurd.backends import get_absurd_backends
from django_absurd.tasks import run_cleanup


class Command(BaseCommand):
    help = "Delete expired task and event history per each queue's retention policy."

    def handle(self, *args: object, **options: object) -> None:
        if not get_absurd_backends():
            self.stdout.write("No Absurd task backends configured.")
            return
        for row in run_cleanup():
            self.stdout.write(
                f"{row['queue_name']}: "
                f"{row['tasks_deleted']} tasks, {row['events_deleted']} events deleted"
            )
