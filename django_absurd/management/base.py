from django.core.management.base import BaseCommand, CommandError

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.queues import SyncResult


def resolve_backend(options: dict) -> tuple[str, AbsurdBackend]:
    backends = get_absurd_backends()
    alias = options["alias"]
    if alias is not None:
        if alias not in backends:
            valid = ", ".join(sorted(backends))
            msg = f"'{alias}' is not an Absurd backend alias. Valid aliases: {valid}"
            raise CommandError(msg)
        backend = backends[alias]
    elif len(backends) == 1:
        alias, backend = next(iter(backends.items()))
    else:
        aliases = ", ".join(sorted(backends))
        msg = f"Multiple Absurd backends found: {aliases}. Use --alias to select one."
        raise CommandError(msg)
    return alias, backend


class AbsurdReportCommand(BaseCommand):
    """Base for commands that report a queue SyncResult to stdout/stderr."""

    def report_sync_result(
        self, result: SyncResult, prefix: str = "", empty_message: str | None = None
    ) -> None:
        if result.created:
            self.stdout.write(f"{prefix}Created: {', '.join(result.created)}")
        if result.reconciled:
            self.stdout.write(f"{prefix}Reconciled: {', '.join(result.reconciled)}")
        if empty_message and not result.created and not result.reconciled:
            self.stdout.write(f"{prefix}{empty_message}")
        for warning in result.storage_warnings:
            self.stderr.write(self.style.WARNING(f"{prefix}{warning}"))
