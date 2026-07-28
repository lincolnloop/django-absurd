import asyncio
import logging
import typing as t

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connections
from django.tasks import Task, task

from django_absurd import absurd_params
from django_absurd.connection import register_jsonb_loader
from django_absurd.queues import get_absurd_client
from django_absurd.tasks import AbsurdTask
from tests import tasks

if t.TYPE_CHECKING:
    from absurd_sdk import JsonObject, RetryStrategy

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


@task(backend="immediate")
def make_group_on_immediate_backend(name: str) -> str:
    Group.objects.create(name=name)
    return name


@task(backend="immediate")
def multiply_on_immediate_backend(a: int, b: int) -> int:
    return a * b


def test_bind_returns_a_real_task() -> None:
    bound = absurd_params(max_attempts=3).bind(tasks.add)
    assert isinstance(bound, Task)
    assert isinstance(bound, AbsurdTask)
    assert bound.func is tasks.add.func
    assert isinstance(tasks.add, AbsurdTask)
    assert tasks.add.absurd_params is None


def test_an_empty_call_is_legal_and_adds_nothing() -> None:
    bound = absurd_params().bind(tasks.with_default_attempts)
    assert isinstance(bound, AbsurdTask)
    assert bound.absurd_params == {"max_attempts": 7}


def test_unset_fields_never_enter_the_params() -> None:
    # Exact-dict equality is what pins omission: an unset field must not become a
    # key at all. The claimed payload can't prove this — the SDK drops None values.
    bound = absurd_params(max_attempts=3).bind(tasks.add)
    assert isinstance(bound, AbsurdTask)
    assert bound.absurd_params == {"max_attempts": 3}


def test_bound_task_aenqueues() -> None:
    call_command("absurd_sync_queues")
    asyncio.run(absurd_params(max_attempts=3).bind(tasks.add).aenqueue(1, 2))
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 3


def test_a_later_plain_enqueue_still_sees_the_default() -> None:
    call_command("absurd_sync_queues")
    absurd_params(max_attempts=9).bind(tasks.with_default_attempts).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    client = get_absurd_client()
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 9
    tasks.with_default_attempts.enqueue(1, 2)
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 7


def test_headers_are_copied_away_from_the_caller() -> None:
    # Mutate between bind and enqueue: after enqueue the payload is already in
    # Postgres and the assertion would hold even with every deep-copy deleted.
    call_command("absurd_sync_queues")
    headers: JsonObject = {"trace": "abc"}
    bound = absurd_params(headers=headers).bind(tasks.add)
    headers["trace"] = "mutated-before-enqueue"
    bound.enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["headers"] == {"trace": "abc"}


def test_params_survive_using_in_both_orderings() -> None:
    call_command("absurd_sync_queues")
    register_jsonb_loader(connections["default"].connection)
    client = get_absurd_client()
    absurd_params(max_attempts=9).bind(
        tasks.with_default_attempts.using(priority=0)
    ).enqueue(1, 2)
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 9
    absurd_params(max_attempts=9).bind(tasks.with_default_attempts).using(
        priority=0
    ).enqueue(1, 2)
    assert client.claim_tasks(batch_size=1)[0]["max_attempts"] == 9


def test_binding_before_routing_still_routes() -> None:
    call_command("absurd_sync_queues")
    result = (
        absurd_params(max_attempts=9)
        .bind(tasks.with_default_attempts)
        .using(queue_name="other")
        .enqueue(1, 2)
    )
    assert result.id.startswith("other:")


def test_a_task_from_another_backend_binds_and_spawns() -> None:
    call_command("absurd_sync_queues")
    routed = multiply_on_immediate_backend.using(backend="default")
    assert not isinstance(routed, AbsurdTask)  # kept task_class = Task
    absurd_params(max_attempts=9).bind(routed).enqueue(3, 4)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 9


def test_repeated_binds_merge_with_the_later_value_winning() -> None:
    call_command("absurd_sync_queues")
    strategy: RetryStrategy = {"kind": "none"}
    once = absurd_params(max_attempts=9, retry_strategy=strategy).bind(tasks.add)
    absurd_params(max_attempts=4).bind(once).enqueue(1, 2)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)[0]
    assert claimed["max_attempts"] == 4
    assert claimed["retry_strategy"] == strategy


def test_binding_off_backend_attaches_and_stays_quiet() -> None:
    # bind no longer predicts whether params will apply — it attaches, and the
    # enqueue-time guard observes whether they actually did.
    bound = absurd_params(max_attempts=9).bind(make_group_on_immediate_backend)
    assert isinstance(bound, AbsurdTask)
    assert bound.absurd_params == {"max_attempts": 9}


def test_binding_off_backend_then_routing_in_keeps_the_params() -> None:
    call_command("absurd_sync_queues")
    bound = absurd_params(max_attempts=9).bind(multiply_on_immediate_backend)
    bound.using(backend="default").enqueue(3, 4)
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["max_attempts"] == 9


def test_enqueuing_inert_params_off_backend_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bound = absurd_params(max_attempts=9).bind(make_group_on_immediate_backend)
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        bound.enqueue("off-backend-ran")
        bound.enqueue("off-backend-ran-again")
    assert caplog.messages == [
        "absurd_params ignored: tests.core.test_absurd_params."
        "make_group_on_immediate_backend ran on task backend 'immediate', "
        "which is not an Absurd backend"
    ]
    # The immediate backend still ran the task; only the params were inert.
    assert Group.objects.filter(name="off-backend-ran").exists()
    assert Group.objects.filter(name="off-backend-ran-again").exists()


def test_enqueuing_params_routed_off_backend_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Bound on Absurd, then routed out — silent before this guard existed.
    bound = absurd_params(max_attempts=9).bind(tasks.echo)
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        bound.using(backend="immediate").enqueue("routed-out")
    assert caplog.messages == [
        "absurd_params ignored: tests.tasks.echo ran on task backend 'immediate', "
        "which is not an Absurd backend"
    ]


def test_enqueuing_on_the_absurd_backend_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call_command("absurd_sync_queues")
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        absurd_params(max_attempts=9).bind(tasks.add).enqueue(1, 2)
    assert caplog.messages == []


def test_aenqueuing_inert_params_off_backend_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bound = absurd_params(max_attempts=9).bind(multiply_on_immediate_backend)
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        asyncio.run(bound.aenqueue(3, 4))
    assert caplog.messages == [
        "absurd_params ignored: tests.core.test_absurd_params."
        "multiply_on_immediate_backend ran on task backend 'immediate', "
        "which is not an Absurd backend"
    ]
