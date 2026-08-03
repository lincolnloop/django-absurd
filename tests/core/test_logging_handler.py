import logging
import typing as t

import pytest

from django_absurd import logging as absurd_logging
from django_absurd.test import AbsurdTestRuntime
from tests import tasks


@pytest.fixture(autouse=True)
def _restore_package_logger() -> t.Iterator[None]:
    """Every test here mutates the ``django_absurd`` logger's handlers or level to
    probe ``attach_console_handler``; a leaked handler would fire for every later
    test in the session, so both are restored.
    """
    logger = logging.getLogger("django_absurd")
    handlers = logger.handlers[:]
    level = logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


def test_importing_the_package_attaches_nothing() -> None:
    """A library must not fight the project's LOGGING."""
    assert logging.getLogger("django_absurd").handlers == []


@pytest.mark.django_db(transaction=True)
def test_ready_and_enqueuing_attach_nothing(dj_absurd: AbsurdTestRuntime) -> None:
    """``AppConfig.ready`` already ran by the time this test starts; enqueuing must
    not attach anything either — nothing but the two commands' ``handle()`` does.
    """
    with dj_absurd.freeze_time():
        tasks.add.enqueue(1, 2)
    assert logging.getLogger("django_absurd").handlers == []


def test_attaching_gives_the_package_logger_one_info_handler() -> None:
    logger = logging.getLogger("django_absurd")
    root = logging.getLogger()
    root_handlers = root.handlers[:]
    # pytest's own log-capture handler sits on root for a test's whole call phase;
    # hasHandlers() would see it and treat the project as already configured.
    root.handlers = []
    try:
        absurd_logging.attach_console_handler()
    finally:
        root.handlers = root_handlers
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)
    assert logger.level == logging.INFO


def test_attaching_twice_does_not_duplicate_the_handler() -> None:
    logger = logging.getLogger("django_absurd")
    root = logging.getLogger()
    root_handlers = root.handlers[:]
    root.handlers = []
    try:
        absurd_logging.attach_console_handler()
        absurd_logging.attach_console_handler()
    finally:
        root.handlers = root_handlers
    assert len(logger.handlers) == 1


def test_attaching_defers_to_a_handler_already_on_the_package_logger() -> None:
    logger = logging.getLogger("django_absurd")
    configured = logging.NullHandler()
    logger.addHandler(configured)
    absurd_logging.attach_console_handler()
    assert logger.handlers == [configured]


def test_attaching_defers_to_a_handler_configured_on_root() -> None:
    """Records propagate: a project that configures only root, never
    ``django_absurd`` directly, must not get a second handler added underneath it —
    that would print every line twice.
    """
    logger = logging.getLogger("django_absurd")
    root = logging.getLogger()
    configured = logging.NullHandler()
    root.addHandler(configured)
    try:
        absurd_logging.attach_console_handler()
        assert logger.handlers == []
    finally:
        root.removeHandler(configured)
