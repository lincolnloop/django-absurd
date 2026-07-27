"""Dependency-light leaf: predicates for whether pg_cron scheduling should be inert.

Populated by ``PgCronConfig.ready()`` (``ORIGINAL_DATABASE_NAMES``) and consumed by
``apps.should_sync_schedules`` and the (later) catalog seam. Kept import-cycle-free:
this module never imports ``django_absurd.pg_cron.apps``.
"""

from django.db import connections

from django_absurd.backends import get_absurd_backends

try:
    from django.test.utils import _TestState
except ImportError as exc:  # pragma: no cover
    msg = (
        "django-absurd expected django.test.utils._TestState to exist so it could "
        "detect an active test environment, but that attribute is absent on this "
        "Django version. django-absurd's pg_cron test-detection needs to be updated "
        "for this Django release."
    )
    raise RuntimeError(msg) from exc

ORIGINAL_DATABASE_NAMES: dict[str, str] = {}


def test_environment_active() -> bool:
    return hasattr(_TestState, "saved_data")


def is_test_database(alias: str) -> bool:
    live_name = str(connections[alias].settings_dict["NAME"])
    return live_name != ORIGINAL_DATABASE_NAMES.get(alias)


def is_pg_cron_inert(alias: str) -> bool:
    return (
        test_environment_active() or is_test_database(alias)
    ) and not pg_cron_on_test_db(alias)


def pg_cron_on_test_db(alias: str) -> bool:
    for backend in get_absurd_backends().values():
        if backend.database == alias:
            return bool(backend.options.get("PG_CRON_ON_TEST_DB", False))
    return False
