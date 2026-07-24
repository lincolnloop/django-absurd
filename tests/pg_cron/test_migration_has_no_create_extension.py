from django.contrib.postgres.operations import CreateExtension
from django.db import connections
from django.db.migrations.loader import MigrationLoader


def test_initial_migration_declares_no_create_extension() -> None:
    loader = MigrationLoader(connections["default"])
    migration = loader.get_migration("django_absurd_pg_cron", "0001_initial")
    assert not any(isinstance(op, CreateExtension) for op in migration.operations)
