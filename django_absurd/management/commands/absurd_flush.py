from django.apps import apps
from django.core.management.base import CommandParser

from django_absurd.backends import PG_CRON_APP_NAME, get_absurd_backends
from django_absurd.exceptions import BackendNotConfiguredError
from django_absurd.flush import clear_provisioned_queues, teardown_owned_pg_cron_jobs
from django_absurd.management.base import AbsurdCommand
from django_absurd.queues import list_provisioned_queues

PG_CRON_WARNING = (
    "This will UNSCHEDULE django-absurd's pg_cron jobs and delete ALL schedule rows,"
    " including admin-authored ones."
)


class Command(AbsurdCommand):
    help = (
        "Drop ALL Absurd queues and their data, and — with django_absurd.pg_cron"
        " installed — unschedule every pg_cron job django-absurd owns (the schema and"
        " functions are kept). Destructive."
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
            raise BackendNotConfiguredError
        queues = list_provisioned_queues()
        pg_cron_installed = apps.is_installed(PG_CRON_APP_NAME)
        # Zero queues is not nothing to do under pg_cron: the CLEANUP job is invisible
        # to this count and outlives every queue.
        if not queues and not pg_cron_installed:
            self.stdout.write("No queues to flush.")
            return
        names = ", ".join(queues)
        if options["interactive"]:
            if queues:
                self.stdout.write(
                    f"This will DROP {len(queues)} queue(s) and ALL their data: {names}"
                )
            if pg_cron_installed:
                self.stdout.write(PG_CRON_WARNING)
            try:
                confirm = input("Type 'yes' to continue, or 'no' to cancel: ")
            except EOFError:  # non-interactive (CI, docker exec -T) — cancel
                confirm = "no"
        else:
            confirm = "yes"
        if confirm != "yes":
            self.stdout.write("Flush cancelled.")
            return
        commands = ["manage.py absurd_sync_queues"]
        if pg_cron_installed:
            # Before the queues go, not after: a job firing in the window between
            # would spawn into a queue that no longer exists.
            removed = teardown_owned_pg_cron_jobs()
            commands.append("manage.py absurd_sync_crons")
            self.stdout.write(
                "Unscheduled django-absurd's pg_cron jobs and removed"
                f" {removed} schedule row(s)."
            )
        if queues:
            # Reported off what came back, not off the pre-prompt listing: the two can
            # differ, and only the first is what actually went.
            dropped = clear_provisioned_queues(drop_schema=True)
            self.stdout.write(f"Dropped {len(dropped)} queue(s): {', '.join(dropped)}")
        self.stdout.write(f"Re-provision with: {', '.join(commands)}")
