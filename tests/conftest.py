import logging
import typing as t

import pytest
from django.contrib.auth.models import User

from django_absurd.flush import flush_absurd_state


@pytest.fixture(autouse=True)
def _enable_db(db: None) -> None:
    pass


@pytest.fixture(autouse=True)
def _restore_absurd_logger_state() -> t.Iterator[None]:
    """Undo whatever a real ``absurd_worker``/``absurd_beat`` invocation, or a
    logging-handler test that deliberately hides pytest's own ambient handler, left on
    the actual ``django_absurd`` logger.

    ``attach_console_handler`` mutates that logger for real, process-wide, with no
    test-scoped isolation of its own — that permanence is the point in production. In
    an ordinary test, though, its handler branch never actually fires: pytest attaches
    its own log-capture handler to root fresh for every call phase, which already
    satisfies ``hasHandlers()``, so a real command invoked through ``call_command``
    only ever moves the level (gated on ``NOTSET`` alone) to INFO — permanently, for
    the rest of the session, changing what a later, unrelated test observes through
    ``caplog`` at the module's normally-unconfigured level. Handlers are restored too
    because ``tests/core/test_logging_handler.py`` deliberately hides that ambient
    handler to exercise the real attach path, and does leave a real one behind.
    """
    logger = logging.getLogger("django_absurd")
    handlers = logger.handlers[:]
    level = logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


@pytest.fixture
def _isolate_queues(_enable_db: None) -> t.Iterator[None]:
    """Hard-drop all Absurd queue topology around a test (before AND after).

    The auto cleanup installed by the pytest plugin only TRUNCATEs after each DB
    test — it removes rows, not queues. Topology-varying files create/vary queues
    whose per-queue tables (DDL) and ``managed=False`` registry rows survive a
    truncate and leak across ``--reuse-db`` runs. Apply this (via a module-level
    ``pytest.mark.usefixtures("_isolate_queues")``) only to those files to drop the
    schema on both sides so each such test is hermetic.

    Naming: ``_``-prefixed but non-autouse. Outside the LETTER of CLAUDE.md's
    autouse-only underscore exception, but within its spirit — never called
    directly, only applied via ``usefixtures``.
    """
    flush_absurd_state(drop_schema=True)
    yield
    flush_absurd_state(drop_schema=True)


@pytest.fixture
def admin_user() -> User:
    # No password: tests log in via force_login (never checks it), and setting one
    # runs the deliberately-slow PBKDF2 hasher on every fixture use.
    return User.objects.create_superuser("admin", "a@x.com")


@pytest.fixture
def staff_user() -> User:
    return User.objects.create_user("staff", "s@x.com", is_staff=True)
