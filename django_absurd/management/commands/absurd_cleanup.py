import typing as t

from django.core.management.base import CommandParser

from django_absurd.backends import get_absurd_backends
from django_absurd.cleanup import cleanup_queues
from django_absurd.exceptions import BackendNotConfiguredError
from django_absurd.management.base import AbsurdCommand


class Command(AbsurdCommand):
    help = "Delete expired task and event history per each queue's retention policy."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "queues",
            nargs="*",
            help="Queue names to clean up; omit to clean every queue.",
        )

    def handle(self, *args: object, **options: object) -> None:
        if not get_absurd_backends():
            raise BackendNotConfiguredError(0)
        queues = t.cast("list[str]", options["queues"]) or None
        for row in cleanup_queues(queues):
            self.stdout.write(
                f"{row['queue_name']}: "
                f"{row['tasks_deleted']} tasks, {row['events_deleted']} events deleted"
            )
