import importlib
import typing as t

import pytest
from django.core.management import call_command
from django.db import connections
from django.test import TransactionTestCase

if t.TYPE_CHECKING:
    import collections.abc

    import pytest_django.fixtures

from django_absurd import pytest_plugin
from django_absurd.flush import flush_absurd_state
from django_absurd.models import Queue, Task
from django_absurd.params import AbsurdSpawnParams
from django_absurd.test import install_absurd_cleanup
from tests import utils
from tests.tasks import add

pytestmark = pytest.mark.django_db(transaction=True)

# django-stubs doesn't model TransactionTestCase's internal ``_post_teardown`` hook;
# access it (and probe instances) through this alias to keep the mechanism assertions
# honest without a stub-gap ``type: ignore`` on every line.
TxnCase: t.Any = TransactionTestCase


def test_flush_absurd_state_truncates_rows_by_default() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1

    flush_absurd_state()

    assert task_model.objects.filter(queue="default").count() == 0
    assert Queue.objects.filter(queue_name="default").exists()  # schema untouched


def test_flush_absurd_state_truncates_a_partitioned_queues_idempotency_table(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # `i_<queue>` only exists for a `partitioned` queue — exercise that branch of
    # truncate_queue_tables (the unpartitioned "default" queue used elsewhere in this
    # file never creates it).
    settings.TASKS = utils.make_tasks_settings(
        queues={"part": {"storage_mode": "partitioned"}}
    )
    call_command("absurd_sync_queues")
    add.using(queue_name="part").enqueue(  # type: ignore[call-arg]
        1, 2, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="k")
    )
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="part").count() == 1

    flush_absurd_state()

    assert task_model.objects.filter(queue="part").count() == 0
    assert Queue.objects.filter(queue_name="part").exists()  # schema untouched


def test_flush_absurd_state_drops_schema_when_requested() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)

    flush_absurd_state(drop_schema=True)

    assert not Queue.objects.filter(queue_name="default").exists()


def test_flush_absurd_state_is_a_noop_on_an_unmigrated_schema(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # Mirrors tests/core/test_checks.py::test_db_unreachable_is_silent's real
    # unreachable-DB technique: mutating settings.DATABASES alone is not enough — the
    # existing psycopg connection stays open and gets reused. del connections["default"]
    # forces the NEXT use to actually attempt (and fail) a fresh connect against the
    # bogus name, which is what makes OperationalError/ProgrammingError reachable here.
    real_name = settings.DATABASES["default"]["NAME"]
    settings.DATABASES["default"]["NAME"] = "absurd_nope_missing_db"
    del connections["default"]
    try:
        flush_absurd_state()  # must not raise
    finally:
        settings.DATABASES["default"]["NAME"] = real_name
        connections["default"].close()


def test_post_teardown_hook_truncates_absurd_state() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1

    class ProbeCase(TransactionTestCase):
        databases = {"default"}

    probe: t.Any = ProbeCase()
    probe._post_teardown()

    assert task_model.objects.filter(queue="default").count() == 0
    assert Queue.objects.filter(queue_name="default").exists()  # truncate, not drop


def test_post_teardown_hook_skips_undeclared_absurd_alias() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)

    class NoDatabasesCase(TransactionTestCase):
        databases = set[str]()

    probe: t.Any = NoDatabasesCase()
    probe._post_teardown()

    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1  # guard skipped it


def test_post_teardown_hook_skips_without_an_absurd_backend(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # Seed under the real Absurd backend, then swap TASKS to a non-Absurd backend so
    # the hook's unconfigured-backend guard (no AbsurdBackend) fires and skips flushing.
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1

    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }

    class ProbeCase(TransactionTestCase):
        databases = {"default"}

    probe: t.Any = ProbeCase()
    probe._post_teardown()

    assert task_model.objects.filter(queue="default").count() == 1  # guard skipped it


def test_install_absurd_cleanup_is_idempotent() -> None:
    already_installed = TxnCase._post_teardown

    install_absurd_cleanup()

    assert TxnCase._post_teardown is already_installed


def test_install_absurd_cleanup_wraps_a_fresh_post_teardown() -> None:
    installed = TxnCase._post_teardown
    original = installed.__wrapped__
    TxnCase._post_teardown = original
    try:
        install_absurd_cleanup()

        assert TxnCase._post_teardown is not original
        assert TxnCase._post_teardown.__wrapped__ is original
    finally:
        TxnCase._post_teardown = installed


def test_absurd_drain_queue_processes_an_enqueued_task(
    absurd_drain_queue: "collections.abc.Callable[..., None]",
) -> None:
    call_command("absurd_sync_queues")
    result = add.enqueue(3, 4)
    absurd_drain_queue()
    snap = utils.get_task_result(result.id)
    assert snap is not None
    assert snap.state == "completed"


def test_plugin_module_imports_cleanly() -> None:
    # The pytest11 plugin module loads during pytest's bootstrap, before pytest-cov
    # starts, so its module-level lines escape the coverage session. Reloading
    # re-executes them under coverage — and doubles as a smoke test that the shipped
    # entry-point module imports cleanly and exposes its public surface.
    importlib.reload(pytest_plugin)
    assert callable(pytest_plugin.pytest_configure)
    assert callable(pytest_plugin.absurd_drain_queue)
