import logging

import pytest
import pytest_django.fixtures
from django.core.management import call_command

from django_absurd import logging as absurd_logging
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

CONSOLE_LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"django_absurd": {"handlers": ["console"], "level": "INFO"}},
}


def test_importing_the_package_attaches_nothing() -> None:
    """A library must not fight the project's LOGGING."""
    logger = logging.getLogger("django_absurd")
    assert logger.handlers == []
    assert logger.level == logging.NOTSET


@pytest.mark.django_db(transaction=True)
def test_enqueuing_attaches_nothing(dj_absurd: AbsurdTestRuntime) -> None:
    with dj_absurd.freeze_time():
        tasks.add.enqueue(1, 2)
    logger = logging.getLogger("django_absurd")
    assert logger.handlers == []
    assert logger.level == logging.NOTSET


@pytest.mark.django_db(transaction=True)
def test_running_the_worker_gives_the_package_a_console_handler() -> None:
    call_command("absurd_worker", queue="default", burst=True)
    logger = logging.getLogger("django_absurd")
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)
    assert logger.level == logging.INFO


@pytest.mark.django_db(transaction=True)
def test_the_worker_defers_to_a_project_that_configured_this_package(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.LOGGING = CONSOLE_LOGGING
    call_command("absurd_worker", queue="default", burst=True)
    assert logging.getLogger("django_absurd").handlers == []


def test_a_configured_child_logger_also_counts() -> None:
    """Configuring django_absurd.worker says what you want from the worker; a handler
    on the parent would print those lines twice.
    """
    assert absurd_logging.declares_absurd_logger(
        {"loggers": {"django_absurd.worker": {"level": "DEBUG"}}}
    )


@pytest.mark.parametrize(
    "config",
    [None, {}, {"loggers": {}}, {"loggers": None}, {"loggers": {"django": {}}}],
)
def test_an_unrelated_logging_config_does_not_count(config: object) -> None:
    assert not absurd_logging.declares_absurd_logger(config)


@pytest.mark.django_db(transaction=True)
def test_attaching_twice_does_not_duplicate_the_handler(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = utils.make_tasks_settings()
    absurd_logging.attach_console_handler()
    absurd_logging.attach_console_handler()
    assert len(logging.getLogger("django_absurd").handlers) == 1
