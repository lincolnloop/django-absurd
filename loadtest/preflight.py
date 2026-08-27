"""What every probe asserts about the database before it measures anything.

The load database is persistent and is nobody's test database, so nothing re-migrates
it: it kept its schema across an upstream Absurd release and every probe went on
reporting numbers for a version the code no longer ships. The tables existed, the admin
did not care, and the only guard that would have noticed — ``load_seed``'s column drift
check — fires on a seed, which nobody had re-run.
"""

import typing as t

from django.core.management import CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

from django_absurd.queues import resolve_absurd_database


def require_migrated_database(using: str | None = None) -> None:
    """Refuse to probe a database whose migrations are not fully applied."""
    alias = using or resolve_absurd_database()
    pending = find_unapplied_migrations(alias)
    if not pending:
        return

    apps = ", ".join(sorted({app_label for app_label, _ in pending}))
    msg = (
        f"The '{alias}' database has {len(pending)} unapplied migration(s) "
        f"({apps}). A probe measures whatever the schema happens to be, so it would "
        "report numbers for a version this checkout does not ship. Run "
        "`PGPORT=5436 python -m loadtest.manage migrate` first — and if that fails "
        "because the objects already exist under an older history, rebuild the "
        "database with `docker compose -f loadtest/compose.yaml down -v` and reseed."
    )
    raise CommandError(msg)


def find_unapplied_migrations(alias: str) -> "list[tuple[str, str]]":
    """The (app_label, name) of every migration the database is missing."""
    executor = MigrationExecutor(connections[alias])
    targets = executor.loader.graph.leaf_nodes()
    plan: t.Iterable[t.Any] = executor.migration_plan(targets)
    return [(migration.app_label, migration.name) for migration, _ in plan]
