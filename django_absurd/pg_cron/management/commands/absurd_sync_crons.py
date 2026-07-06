import typing as t

from django.core.management.base import BaseCommand, CommandError

from django_absurd.management.base import resolve_backend
from django_absurd.pg_cron.reconcile import sync_crons, teardown_crons


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
            help="Remove all owned pg_cron jobs and settings ScheduledTask rows.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        alias, backend = resolve_backend(options)

        if options["teardown"]:
            removed = teardown_crons(backend)
            self.stdout.write(f"Removed {removed} cron(s) — backend '{alias}'.")
            return

        if backend.scheduler != "pg_cron":
            msg = (
                f"SCHEDULER is '{backend.scheduler}', not 'pg_cron' — "
                "absurd_sync_crons only applies to pg_cron backends."
            )
            raise CommandError(msg)

        upserted, pruned = sync_crons(backend)
        self.stdout.write(
            f"Synced {upserted} cron(s); pruned {pruned} — backend '{alias}'."
        )
