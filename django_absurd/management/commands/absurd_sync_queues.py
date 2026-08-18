import typing as t

from django.core.management.base import CommandError

from django_absurd import console
from django_absurd.backends import get_absurd_backends
from django_absurd.management.base import AbsurdCommand
from django_absurd.queues import SyncResult, plan_queue_sync, provision_backend

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser

NOT_IN_SYNC = "Queues are not in sync. Run: manage.py absurd_sync_queues"


class Command(AbsurdCommand):
    help = "Create and reconcile queues declared on each Absurd task backend."

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "Report what would change and exit non-zero if anything would, "
                "without writing. For a release step that asserts rather than acts."
            ),
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        backends = get_absurd_backends()
        if not backends:
            self.stdout.write("No Absurd task backends configured.")
            return
        dry_run = options["check"]
        crate_out = console.build_glyph_prefix(self.stdout, "🗃️")
        crate_err = console.build_glyph_prefix(self.stderr, "🗃️")
        pending = False
        for alias, backend in backends.items():
            alias_label = f"[{alias}] " if len(backends) > 1 else ""
            result = plan_queue_sync(backend) if dry_run else provision_backend(backend)
            pending = pending or bool(
                result.created or result.reconciled or result.repaired
            )
            self.report_sync_result(
                result,
                stdout_prefix=f"{crate_out}{alias_label}",
                stderr_prefix=f"{crate_err}{alias_label}",
                empty_message="No queues to sync.",
                dry_run=dry_run,
            )
        if dry_run and pending:
            raise CommandError(NOT_IN_SYNC)

    def report_sync_result(
        self,
        result: SyncResult,
        *,
        stdout_prefix: str,
        stderr_prefix: str,
        empty_message: str,
        dry_run: bool = False,
    ) -> None:
        created, reconciled, repaired = (
            ("Would create", "Would reconcile", "Would repair")
            if dry_run
            else ("Created", "Reconciled", "Repaired")
        )
        if result.created:
            self.stdout.write(f"{stdout_prefix}{created}: {', '.join(result.created)}")
        if result.reconciled:
            self.stdout.write(
                f"{stdout_prefix}{reconciled}: {', '.join(result.reconciled)}"
            )
        if result.repaired:
            self.stdout.write(
                f"{stdout_prefix}{repaired}: {', '.join(result.repaired)}"
            )
        if not result.created and not result.reconciled and not result.repaired:
            self.stdout.write(f"{stdout_prefix}{empty_message}")
        for warning in result.storage_warnings:
            self.stderr.write(self.style.WARNING(f"{stderr_prefix}{warning}"))
