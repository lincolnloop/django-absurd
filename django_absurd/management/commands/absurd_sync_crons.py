import typing as t

from django.core.management.base import BaseCommand, CommandError

from django_absurd.management.base import resolve_backend
from django_absurd.pgcron import sync_crons, teardown_crons
from django_absurd.scheduler import get_settings_schedules


class Command(BaseCommand):
    help = "Reconcile pg_cron jobs for all declared SCHEDULE entries."

    def add_arguments(self, parser: t.Any) -> None:
        parser.add_argument(
            "--alias",
            default=None,
            help="Absurd backend alias (required when multiple Absurd backends exist).",
        )
        parser.add_argument(
            "--teardown",
            action="store_true",
            help="Remove all owned pg_cron jobs and settings ScheduledJob rows.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        alias, backend = resolve_backend(options)

        if options["teardown"]:
            teardown_crons(backend)
            self.stdout.write(f"Removed pg_cron jobs for backend '{alias}'.")
            return

        if backend.scheduler != "pg_cron":
            msg = (
                f"SCHEDULER is '{backend.scheduler}', not 'pg_cron' — "
                "absurd_sync_crons only applies to pg_cron backends."
            )
            raise CommandError(msg)

        schedules = get_settings_schedules(backend)
        sync_crons(backend)
        self.stdout.write(f"Synced {len(schedules)} cron job(s) for backend '{alias}'.")
