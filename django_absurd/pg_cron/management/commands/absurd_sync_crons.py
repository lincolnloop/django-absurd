import typing as t

from django.core.management.base import BaseCommand, CommandError

from django_absurd.management.base import resolve_backend
from django_absurd.pg_cron.reconcile import (
    sync_admin_crons,
    sync_crons,
    teardown_crons,
)

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = "Reconcile pg_cron jobs for all declared SCHEDULE entries."

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--teardown",
            action="store_true",
            help="Remove all owned pg_cron jobs and settings ScheduledTask rows.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_true",
            dest="no_input",
            help="Skip the teardown confirmation prompt.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> str | None:
        backend = resolve_backend()

        if options["teardown"]:
            if not options["no_input"] and not self.confirm_teardown(backend.alias):
                self.stdout.write("Aborted.")
                return None
            removed = teardown_crons(include_admin=True)
            self.stdout.write(
                f"Unscheduled all pg_cron jobs and removed {removed} schedule row(s) "
                f"— backend '{backend.alias}'."
            )
            return None

        if backend.scheduler != "pg_cron":
            msg = (
                f"SCHEDULER is '{backend.scheduler}', not 'pg_cron' — "
                "absurd_sync_crons only applies to pg_cron backends."
            )
            raise CommandError(msg)

        try:
            created, pruned = sync_crons(backend)
            sync_admin_crons()
        except KeyError as exc:
            msg = (
                f"SCHEDULE entry is missing required key {exc} — "
                "run `manage.py check` for the E007 details."
            )
            raise CommandError(msg) from exc
        self.stdout.write(
            f"Synced {created} cron(s); pruned {pruned} — backend '{backend.alias}'."
        )
        return None

    def confirm_teardown(self, alias: str) -> bool:
        prompt = (
            f"This unschedules ALL pg_cron jobs for backend '{alias}', including "
            "admin-authored schedules. Proceed? [y/N] "
        )
        try:
            answer = input(prompt)
        except EOFError:  # non-interactive (CI, `docker exec -T`) — abort, don't crash
            return False
        return answer.strip().lower() in ("y", "yes")
