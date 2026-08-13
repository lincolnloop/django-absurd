import typing as t

import pytest
from django.db import connections
from pytest_django.fixtures import Settings

from django_absurd.pg_cron import detection
from tests.pg_cron import utils


@pytest.fixture
def _restore_original_names() -> t.Iterator[None]:
    # tests mutate the module-level snapshot; restore it so the migrate gate (which keys
    # on it) isn't corrupted for the rest of the session.
    saved = dict(detection.ORIGINAL_DATABASE_NAMES)
    try:
        yield
    finally:
        detection.ORIGINAL_DATABASE_NAMES.clear()
        detection.ORIGINAL_DATABASE_NAMES.update(saved)


def test_test_environment_active_true_under_pytest() -> None:
    # setup_test_environment ran → the signal is present for the whole suite.
    assert detection.test_environment_active() is True


@pytest.mark.usefixtures("_restore_original_names")
def test_is_test_database_true_when_live_name_differs_from_snapshot() -> None:
    alias = "default"
    detection.ORIGINAL_DATABASE_NAMES[alias] = "some_prod_name"
    assert connections[alias].settings_dict["NAME"] != "some_prod_name"
    assert detection.is_test_database(alias) is True


@pytest.mark.usefixtures("_restore_original_names")
def test_is_test_database_false_when_live_name_matches_snapshot() -> None:
    alias = "default"
    live_name = str(connections[alias].settings_dict["NAME"])
    detection.ORIGINAL_DATABASE_NAMES[alias] = live_name
    assert detection.is_test_database(alias) is False


def test_is_pg_cron_inert_true_under_tests_without_opt_in(
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    assert detection.is_pg_cron_inert("default") is True


def test_is_pg_cron_inert_false_when_opt_in(settings: Settings) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    assert detection.is_pg_cron_inert("default") is False


def test_is_pg_cron_inert_true_for_alias_without_a_backend_even_when_opted_in(
    settings: Settings,
) -> None:
    # "replica" has no configured backend of its own (only "default" does), so its
    # opt-in never applies — it stays inert regardless of "default"'s setting.
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    assert detection.is_pg_cron_inert("replica") is True
