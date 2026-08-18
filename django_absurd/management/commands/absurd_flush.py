from django.core.management.base import CommandParser

from django_absurd.backends import get_absurd_backends
from django_absurd.exceptions import BackendNotConfiguredError
from django_absurd.flush import clear_queues
from django_absurd.management.base import AbsurdCommand
from django_absurd.queues import list_provisioned_queues


class Command(AbsurdCommand):
    help = (
        "Drop ALL Absurd queues and their data (the schema and functions are kept)."
        " Destructive."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Tells Django to NOT prompt the user for input of any kind.",
        )

    def handle(self, *args: object, **options: object) -> None:
        if not get_absurd_backends():
            raise BackendNotConfiguredError(0)
        queues = list_provisioned_queues()
        if not queues:
            self.stdout.write("No queues to flush.")
            return
        names = ", ".join(queues)
        if options["interactive"]:
            self.stdout.write(
                f"This will DROP {len(queues)} queue(s) and ALL their data: {names}"
            )
            try:
                confirm = input("Type 'yes' to continue, or 'no' to cancel: ")
            except EOFError:  # non-interactive (CI, docker exec -T) — cancel
                confirm = "no"
        else:
            confirm = "yes"
        if confirm != "yes":
            self.stdout.write("Flush cancelled.")
            return
        clear_queues(drop_schema=True)
        self.stdout.write(f"Dropped {len(queues)} queue(s): {names}")
