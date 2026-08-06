import inspect
import logging

import pytest
import pytest_django.fixtures

from django_absurd import hooks
from django_absurd import logging as absurd_logging
from django_absurd.queues import get_absurd_client
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
    utils.run_worker_command_until(queue="default")
    logger = logging.getLogger("django_absurd")
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)
    assert logger.level == logging.INFO


@pytest.mark.django_db(transaction=True)
def test_the_worker_defers_to_a_project_that_configured_this_package(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.LOGGING = CONSOLE_LOGGING
    utils.run_worker_command_until(queue="default")
    assert logging.getLogger("django_absurd").handlers == []


def test_a_configured_child_logger_is_left_alone(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    """Configuring django_absurd.worker says what you want from the worker; a handler
    on the parent would print those lines twice.
    """
    settings.LOGGING = {"loggers": {"django_absurd.worker": {"level": "DEBUG"}}}
    absurd_logging.attach_console_handler()
    assert logging.getLogger("django_absurd").handlers == []


@pytest.mark.parametrize(
    "config",
    [None, {}, {"loggers": None}, {"loggers": {}}, {"loggers": {"django": {}}}],
)
def test_a_logging_config_that_names_someone_else_still_gets_the_default(
    config: object, settings: pytest_django.fixtures.SettingsWrapper
) -> None:
    settings.LOGGING = config
    absurd_logging.attach_console_handler()
    assert len(logging.getLogger("django_absurd").handlers) == 1


@pytest.mark.django_db(transaction=True)
def test_attaching_twice_does_not_duplicate_the_handler(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = utils.make_tasks_settings()
    absurd_logging.attach_console_handler()
    absurd_logging.attach_console_handler()
    assert len(logging.getLogger("django_absurd").handlers) == 1


def test_the_sync_client_gets_only_the_hook_it_can_run() -> None:
    """The sync ``Absurd`` client's own ``_execute_task`` never awaits a hook's return
    value (unlike the async path, which checks ``inspect.isawaitable``), so handing it
    ``wrap_task_execution`` — an ``async def`` — would hand back an un-awaited coroutine
    as the run's own result. The async client, built in ``worker.py``, takes both.
    """
    client = get_absurd_client()
    assert set(client._hooks) == {"before_spawn"}
    assert client._hooks["before_spawn"] is hooks.log_before_spawn
    assert inspect.iscoroutinefunction(hooks.log_task_execution)


def test_the_worker_defers_the_handler_to_a_root_only_logging_config(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    """Root catches our records by propagation, so attaching underneath it would
    print every line twice — once bare, once through the project's handler.
    """
    settings.LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "root": {"handlers": ["console"], "level": "WARNING"},
    }
    absurd_logging.attach_console_handler()
    logger = logging.getLogger("django_absurd")
    assert logger.handlers == []
    # Still raised: root's WARNING would otherwise filter our INFO lines out before
    # its own handler ever saw them.
    assert logger.level == logging.INFO


def test_a_root_entry_spelled_under_loggers_counts_too(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.LOGGING = {
        "version": 1,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {"": {"handlers": ["console"]}},
    }
    absurd_logging.attach_console_handler()
    assert logging.getLogger("django_absurd").handlers == []


def test_a_level_set_in_code_is_not_overwritten() -> None:
    """A project that silenced us without a LOGGING entry stays silenced."""
    logger = logging.getLogger("django_absurd")
    logger.setLevel(logging.ERROR)
    absurd_logging.attach_console_handler()
    assert logger.handlers == []
    assert logger.level == logging.ERROR


def test_a_handler_attached_in_code_is_left_alone() -> None:
    logger = logging.getLogger("django_absurd")
    installed = logging.NullHandler()
    logger.addHandler(installed)
    absurd_logging.attach_console_handler()
    assert logger.handlers == [installed]
