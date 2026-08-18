from django_absurd import console
from django_absurd.backends import get_absurd_backends
from django_absurd.management.base import AbsurdCommand
from django_absurd.queues import SyncResult, provision_backend


class Command(AbsurdCommand):
    help = "Create and reconcile queues declared on each Absurd task backend."

    def handle(self, *args: object, **options: object) -> None:
        backends = get_absurd_backends()
        if not backends:
            self.stdout.write("No Absurd task backends configured.")
            return
        crate_out = console.build_glyph_prefix(self.stdout, "🗃️")
        crate_err = console.build_glyph_prefix(self.stderr, "🗃️")
        for alias, backend in backends.items():
            alias_label = f"[{alias}] " if len(backends) > 1 else ""
            self.report_sync_result(
                provision_backend(backend),
                stdout_prefix=f"{crate_out}{alias_label}",
                stderr_prefix=f"{crate_err}{alias_label}",
                empty_message="No queues to sync.",
            )

    def report_sync_result(
        self,
        result: SyncResult,
        *,
        stdout_prefix: str,
        stderr_prefix: str,
        empty_message: str,
    ) -> None:
        if result.created:
            self.stdout.write(f"{stdout_prefix}Created: {', '.join(result.created)}")
        if result.reconciled:
            self.stdout.write(
                f"{stdout_prefix}Reconciled: {', '.join(result.reconciled)}"
            )
        if not result.created and not result.reconciled:
            self.stdout.write(f"{stdout_prefix}{empty_message}")
        for warning in result.storage_warnings:
            self.stderr.write(self.style.WARNING(f"{stderr_prefix}{warning}"))
