import asyncio
import typing as t

import psycopg.errors
import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connections, transaction
from django.tasks import TaskResultStatus, task
from django.tasks.exceptions import InvalidTask
from pytest_django import Settings

from django_absurd import absurd_params
from django_absurd.connection import register_jsonb_loader
from django_absurd.exceptions import (
    QueueNotDeclaredError,
    QueueNotProvisionedError,
    SchemaNotInstalledError,
)
from django_absurd.models import Queue
from django_absurd.queues import get_absurd_client
from django_absurd.tasks import AbsurdTask
from tests import tasks, utils

if t.TYPE_CHECKING:
    from absurd_sdk import RetryStrategy

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


@task(backend="immediate")
@absurd_params(max_attempts=4)
def add_on_immediate_backend(a: int, b: int) -> t.Never:
    msg = "routed onto Absurd and claimed, never run by a worker"
    raise NotImplementedError(msg)


def test_decorator_default_survives_a_replace() -> None:
    # .using() is dataclasses.replace(), which re-runs __post_init__ and re-folds.
    call_command("absurd_sync_queues")
    tasks.with_default_attempts.using(priority=0).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 7


def test_a_plain_task_routed_in_keeps_its_decorator_default() -> None:
    # ImmediateBackend keeps task_class = Task and .using() preserves the class, so
    # this reaches enqueue as a bare Task — the getattr branch, not the field branch.
    call_command("absurd_sync_queues")
    routed = add_on_immediate_backend.using(backend="default")
    assert not isinstance(routed, AbsurdTask)
    routed.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 4


def test_enqueue_lands_and_returns_taskresult() -> None:
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(1, 2)
    assert isinstance(result.id, str)
    assert result.id
    assert result.status == TaskResultStatus.READY
    assert result.args == [1, 2]
    assert result.kwargs == {}
    assert result.backend == "default"
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert len(claimed) == 1
    assert claimed[0]["task_name"] == "tests.tasks.add"
    assert claimed[0]["params"] == {"args": [1, 2], "kwargs": {}}


def test_enqueue_preserves_kwargs() -> None:
    call_command("absurd_sync_queues")
    tasks.add.enqueue(a=1, b=2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["params"] == {"args": [], "kwargs": {"a": 1, "b": 2}}


def test_enqueue_rides_django_transaction() -> None:
    call_command("absurd_sync_queues")

    class BoomError(Exception):
        pass

    def enqueue_then_roll_back() -> t.Never:
        with transaction.atomic():
            tasks.add.enqueue(1, 2)
            raise BoomError

    with pytest.raises(BoomError):
        enqueue_then_roll_back()
    register_jsonb_loader(connections["default"].connection)
    assert get_absurd_client().claim_tasks(batch_size=1) == []


def test_undeclared_queue_rejected() -> None:
    call_command("absurd_sync_queues")
    with pytest.raises(InvalidTask):
        tasks.add.using(queue_name="nope").enqueue(1, 2)


def test_aenqueue_lands() -> None:
    call_command("absurd_sync_queues")
    asyncio.run(tasks.add.aenqueue(1, 2))
    register_jsonb_loader(connections["default"].connection)
    assert len(get_absurd_client().claim_tasks(batch_size=1)) == 1


def test_enqueue_to_an_unprovisioned_queue_refuses() -> None:
    # 'default' declared, but _isolate_queues dropped its tables and nothing at runtime
    # puts them back.
    with pytest.raises(QueueNotProvisionedError) as exc:
        tasks.add.enqueue(1, 2)
    assert str(exc.value) == (
        "Queue 'default' is declared but its Absurd table is not provisioned. "
        "Run: manage.py absurd_sync_queues"
    )
    assert Queue.objects.filter(queue_name="default").exists() is False


def test_enqueue_lands_after_a_repaired_idempotency_table(
    settings: Settings,
) -> None:
    # i_<queue> gone on its own is what a partial restore leaves, and the refusal above
    # sends the operator to absurd_sync_queues — so that command has to repair it.
    settings.TASKS = utils.make_tasks_settings(
        queues={**utils.DECLARED_QUEUES, "parts": {"storage_mode": "partitioned"}}
    )
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cursor:
        cursor.execute("drop table absurd.i_parts cascade")
    call_command("absurd_sync_queues")
    bound = absurd_params(idempotency_key="k").bind(tasks.add).using(queue_name="parts")
    result = bound.enqueue(1, 2)
    with connections["default"].cursor() as cursor:
        cursor.execute("select idempotency_key, task_id from absurd.i_parts")
        assert [(key, str(task_id)) for key, task_id in cursor.fetchall()] == [
            ("k", result.id.removeprefix("parts:"))
        ]


def test_enqueue_propagates_an_unrelated_undefined_table() -> None:
    # spawn_task reads absurd.queues when an idempotency key is set, and that relation
    # is nobody's queue table — the classifier must not relabel it as provisioning.
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cursor:
        cursor.execute("alter table absurd.queues rename to queues_hidden")
    try:
        with pytest.raises(psycopg.errors.UndefinedTable) as exc:
            absurd_params(idempotency_key="k").bind(tasks.add).enqueue(1, 2)
    finally:
        with connections["default"].cursor() as cursor:
            cursor.execute("alter table absurd.queues_hidden rename to queues")
    assert exc.value.diag.message_primary == 'relation "absurd.queues" does not exist'


def test_enqueue_to_undeclared_queue_raises() -> None:
    # 'ghost' is not in TASKS QUEUES; validate_task raises InvalidTask naming the queue.
    with pytest.raises(InvalidTask, match="ghost"):
        tasks.add.using(queue_name="ghost").enqueue(1, 2)


def test_enqueue_with_empty_queues_reports_undeclared(
    settings: Settings,
) -> None:
    # Empty QUEUES makes validate_task skip its queue check, reaching the backend guard.
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {}},
        }
    }
    with pytest.raises(QueueNotDeclaredError) as exc:
        tasks.add.enqueue(1, 2)
    assert str(exc.value) == (
        "Queue 'default' is not declared for backend 'default'. "
        "Valid queues: (none). "
        "Add it to the QUEUES list in your TASKS backend settings."
    )


def test_enqueue_refusal_leaves_an_outer_atomic_usable() -> None:
    # The savepoint around spawn is what buys this: the refusal rolls back only the
    # spawn, so the enclosing block can still commit its own writes.
    with transaction.atomic():
        with pytest.raises(QueueNotProvisionedError):
            tasks.make_group.enqueue("refused")
        Group.objects.create(name="after-refusal")
    assert Group.objects.filter(name="after-refusal").exists() is True


def test_enqueue_with_absent_schema_raises_clear_error() -> None:
    with (
        utils.hide_absurd_schema(),
        pytest.raises(
            SchemaNotInstalledError,
            match=r"^Absurd schema is not installed\. Run: manage\.py migrate$",
        ),
    ):
        tasks.add.enqueue(1, 2)


def test_max_attempts_uses_backend_default_when_unset() -> None:
    call_command("absurd_sync_queues")
    tasks.add.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 5


def test_max_attempts_uses_custom_backend_default(settings: Settings) -> None:
    # 7 is our own DEFAULT_MAX_ATTEMPTS, not absurd_sdk's own client-level fallback
    # of 5 — pins backends.py's setdefault("max_attempts", self.default_max_attempts).
    settings.TASKS = utils.make_tasks_settings(default_max_attempts=7)
    call_command("absurd_sync_queues")
    tasks.add.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 7


def test_max_attempts_uses_decorator_default() -> None:
    call_command("absurd_sync_queues")
    tasks.with_default_attempts.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 7


def test_retry_strategy_reaches_spawn() -> None:
    call_command("absurd_sync_queues")
    strategy: RetryStrategy = {
        "kind": "fixed",
        "base_seconds": 1.0,
        "factor": 2.0,
        "max_seconds": 10.0,
    }
    absurd_params(retry_strategy=strategy).bind(tasks.add).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["retry_strategy"] == strategy


def test_idempotency_key_dedups() -> None:
    call_command("absurd_sync_queues")
    bound = absurd_params(idempotency_key="dup").bind(tasks.add)
    r1 = bound.enqueue(1, 2)
    r2 = bound.enqueue(1, 2)
    assert r1.id == r2.id
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(
        batch_size=10
    )  # batch>1 to catch a dup row
    assert len(claimed) == 1
    assert claimed[0]["params"] == {"args": [1, 2], "kwargs": {}}


def test_spawn_params_not_passed_to_task_func() -> None:
    call_command("absurd_sync_queues")
    absurd_params(idempotency_key="x").bind(tasks.add).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["params"] == {"args": [1, 2], "kwargs": {}}


def test_result_id_encodes_queue() -> None:
    call_command("absurd_sync_queues")
    result = tasks.add.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    task_id = str(claimed[0]["task_id"])
    assert result.id == f"default:{task_id}"
