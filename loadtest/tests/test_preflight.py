import typing as t

import pytest
from django.core.management import CommandError
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder

from django_absurd.queues import resolve_absurd_database
from loadtest import preflight

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def unapplied_absurd_migrations() -> t.Iterator[None]:
    """Un-record django-absurd's migrations, leaving the tables in place.

    Exactly the state the persistent load database sat in for three weeks: the schema
    objects exist and every probe runs happily, while the recorded history says
    something the code no longer ships.
    """
    recorder = MigrationRecorder(connections[resolve_absurd_database()])
    applied = [
        migration
        for migration in recorder.applied_migrations()
        if migration[0] == "django_absurd"
    ]
    for app_label, name in applied:
        recorder.record_unapplied(app_label, name)
    yield
    for app_label, name in applied:
        recorder.record_applied(app_label, name)


def test_a_migrated_database_passes_preflight() -> None:
    preflight.require_migrated_database()


def test_preflight_refuses_a_database_with_unapplied_migrations(
    unapplied_absurd_migrations: None,
) -> None:
    with pytest.raises(CommandError) as caught:
        preflight.require_migrated_database()

    assert "unapplied migration" in str(caught.value)


def test_preflight_names_the_app_whose_migrations_are_missing(
    unapplied_absurd_migrations: None,
) -> None:
    with pytest.raises(CommandError) as caught:
        preflight.require_migrated_database()

    assert "django_absurd" in str(caught.value)


def test_preflight_says_how_to_fix_it(
    unapplied_absurd_migrations: None,
) -> None:
    with pytest.raises(CommandError) as caught:
        preflight.require_migrated_database()

    assert "python -m loadtest.manage migrate" in str(caught.value)
