import contextlib
import io
import logging
import typing as t

import pytest
from django.core.management import call_command

from django_absurd import hooks
from django_absurd import logging as absurd_logging
from django_absurd.queues import get_absurd_client
from django_absurd.test import AbsurdTestRuntime
from tests import tasks


def test_importing_the_package_attaches_nothing() -> None:
    """A library must not fight the project's LOGGING."""
    logger = logging.getLogger("django_absurd")
    assert logger.handlers == []
    assert logger.level == logging.NOTSET


@pytest.mark.django_db(transaction=True)
def test_ready_and_enqueuing_attach_nothing(dj_absurd: AbsurdTestRuntime) -> None:
    """``AppConfig.ready`` already ran by the time this test starts; enqueuing must
    not attach anything either — nothing but the two commands' ``handle()`` does.
    """
    with dj_absurd.freeze_time():
        tasks.add.enqueue(1, 2)
    logger = logging.getLogger("django_absurd")
    assert logger.handlers == []
    assert logger.level == logging.NOTSET


@pytest.mark.django_db(transaction=True)
def test_running_the_worker_command_attaches_the_console_handler() -> None:
    """The spec asks for this through the command, not the helper directly — a
    dropped call to ``attach_console_handler()`` in ``handle()`` must fail this.
    """
    logger = logging.getLogger("django_absurd")
    with clear_root_handlers():
        call_command("absurd_worker", queue="default", burst=True)
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)
    assert logger.level == logging.INFO


def test_attaching_gives_the_package_logger_one_info_handler() -> None:
    logger = logging.getLogger("django_absurd")
    with clear_root_handlers():
        absurd_logging.attach_console_handler()
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)
    assert logger.level == logging.INFO


def test_attaching_twice_does_not_duplicate_the_handler() -> None:
    logger = logging.getLogger("django_absurd")
    with clear_root_handlers():
        absurd_logging.attach_console_handler()
        absurd_logging.attach_console_handler()
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
    with clear_root_handlers():
        logging.getLogger().addHandler(logging.NullHandler())
        absurd_logging.attach_console_handler()
        assert logger.handlers == []


def test_attaching_lets_an_info_line_reach_a_root_handler_left_at_warning() -> None:
    """A handler on root is not enough by itself: root's default level of WARNING
    would filter every INFO line at the effective-level check on the logger the
    record started from, before it ever reaches that handler.
    """
    root = logging.getLogger()
    root_level = root.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    with clear_root_handlers():
        root.addHandler(handler)
        root.setLevel(logging.WARNING)
        try:
            absurd_logging.attach_console_handler()
            logging.getLogger("django_absurd.worker").info(
                "worker reached root's handler"
            )
        finally:
            root.setLevel(root_level)
    assert logging.getLogger("django_absurd").handlers == []
    assert stream.getvalue() == "worker reached root's handler\n"


def test_attaching_does_not_duplicate_a_line_when_root_is_already_at_info() -> None:
    root = logging.getLogger()
    root_level = root.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    with clear_root_handlers():
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            absurd_logging.attach_console_handler()
            logging.getLogger("django_absurd.worker").info(
                "worker reached root's handler"
            )
        finally:
            root.setLevel(root_level)
    assert logging.getLogger("django_absurd").handlers == []
    assert stream.getvalue() == "worker reached root's handler\n"


def test_attaching_respects_an_explicit_level_already_set() -> None:
    """The level guard applies on the attaching path too, not only when deferring —
    a project's explicit ``django_absurd`` level must survive either way.
    """
    logger = logging.getLogger("django_absurd")
    logger.setLevel(logging.ERROR)
    with clear_root_handlers():
        absurd_logging.attach_console_handler()
    assert len(logger.handlers) == 1
    assert logger.level == logging.ERROR


def test_attaching_does_not_raise_a_debug_projects_effective_level() -> None:
    """The ``NOTSET`` check alone is not enough: a root at DEBUG with
    ``django_absurd`` left unset must keep DEBUG lines visible, not have them raised
    away to INFO.
    """
    root = logging.getLogger()
    root_level = root.level
    logger = logging.getLogger("django_absurd")
    hooks_logger = logging.getLogger("django_absurd.hooks")
    with clear_root_handlers():
        root.setLevel(logging.DEBUG)
        try:
            assert hooks_logger.isEnabledFor(logging.DEBUG)
            absurd_logging.attach_console_handler()
            assert hooks_logger.isEnabledFor(logging.DEBUG)
        finally:
            root.setLevel(root_level)
    assert logger.level == logging.NOTSET


def test_attaching_defers_to_a_handler_configured_on_a_child_logger() -> None:
    """Records propagate the other direction too: a project that configured only
    ``django_absurd.hooks``, never ``django_absurd`` itself, must not get a handler
    added above it — that would print every one of ``hooks``'s lines twice.
    """
    logger = logging.getLogger("django_absurd")
    hooks_logger = logging.getLogger("django_absurd.hooks")
    configured = logging.NullHandler()
    hooks_logger.addHandler(configured)
    try:
        with clear_root_handlers():
            absurd_logging.attach_console_handler()
        assert logger.handlers == []
    finally:
        hooks_logger.removeHandler(configured)


def test_attaching_does_not_duplicate_a_line_printed_via_a_child_handler() -> None:
    hooks_logger = logging.getLogger("django_absurd.hooks")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    hooks_logger.addHandler(handler)
    try:
        with clear_root_handlers():
            absurd_logging.attach_console_handler()
            hooks_logger.info("hook line")
        assert logging.getLogger("django_absurd").handlers == []
        assert stream.getvalue() == "hook line\n"
    finally:
        hooks_logger.removeHandler(handler)


def test_attaching_ignores_a_child_handler_that_does_not_propagate() -> None:
    """A child's handler with ``propagate = False`` never reaches a handler placed
    on the parent regardless of what we do, so it must not block us from still
    covering everything else under ``django_absurd``.
    """
    logger = logging.getLogger("django_absurd")
    hooks_logger = logging.getLogger("django_absurd.hooks")
    hooks_logger.addHandler(logging.NullHandler())
    hooks_logger.propagate = False
    try:
        with clear_root_handlers():
            absurd_logging.attach_console_handler()
        assert len(logger.handlers) == 1
    finally:
        hooks_logger.handlers = []
        hooks_logger.propagate = True


def test_attaching_ignores_a_placeholder_left_by_an_ungotten_ancestor() -> None:
    """Requesting a descendant name without ever requesting its own parent leaves a
    ``logging.PlaceHolder`` in ``loggerDict``, not a ``Logger`` — iterating it must
    not raise.
    """
    logging.getLogger("django_absurd.made_up_child.deeper")
    logger = logging.getLogger("django_absurd")
    with clear_root_handlers():
        absurd_logging.attach_console_handler()
    assert len(logger.handlers) == 1


@pytest.mark.django_db(transaction=True)
def test_the_sync_client_gets_only_the_hook_it_can_run() -> None:
    """The sync ``Absurd`` client's own ``_execute_task`` never awaits a hook's
    return value (unlike the async path, which checks ``inspect.isawaitable``), so
    handing it ``wrap_task_execution`` — an ``async def`` — would hand back an
    un-awaited coroutine as the run's own result. The async client, built separately
    in ``worker.py``, still gets ``build_absurd_hooks()``'s full, unmodified recipe.
    """
    client = get_absurd_client()
    assert set(client._hooks) == {"before_spawn"}
    assert client._hooks["before_spawn"] is hooks.log_before_spawn
    assert set(hooks.build_absurd_hooks()) == {"before_spawn", "wrap_task_execution"}


@contextlib.contextmanager
def clear_root_handlers() -> t.Iterator[None]:
    """Hide pytest's own ambient log-capture handler for the block.

    Pytest attaches that handler to root fresh for every test's call phase — a plain
    fixture cannot hide it, because fixture setup runs in the phase pytest wraps
    separately from, and strictly before, the one that attaches it; clearing
    ``root.handlers`` in a fixture is undone before the test body ever runs. Called
    explicitly from inside a test body instead, where the timing is right. Nothing in
    this file reads through ``caplog`` (whose own capture handler lives on this same
    root logger) — every assertion here reads ``django_absurd``'s own state directly,
    or a plain ``StringIO`` handler a test attaches itself.
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    root.handlers = []
    try:
        yield
    finally:
        root.handlers = handlers
