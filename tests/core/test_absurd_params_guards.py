import typing as t

import pytest

from django_absurd import absurd_params
from tests import tasks

if t.TYPE_CHECKING:
    from absurd_sdk import RetryStrategy


def test_unknown_keyword_is_rejected() -> None:
    expected = (
        "'max_attemps' is an invalid argument for absurd_params. Valid arguments: "
        "max_attempts, retry_strategy, cancellation, headers, idempotency_key."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(max_attemps=3)  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_queue_is_rejected_with_a_pointer_to_using() -> None:
    expected = (
        "'queue' cannot be set through absurd_params — queue routing belongs to "
        'Django:\n\n    send_report.using(queue_name="reports")'
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(queue="reports")  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_queue_name_gets_the_same_pointer() -> None:
    expected = (
        "'queue_name' cannot be set through absurd_params — queue routing belongs "
        'to Django:\n\n    send_report.using(queue_name="reports")'
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(queue_name="reports")  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_run_after_is_rejected_with_the_defer_pointer() -> None:
    expected = (
        "'run_after' cannot be set through absurd_params, and deferred enqueue is "
        "not supported by this backend (supports_defer = False). See "
        "https://github.com/lincolnloop/django-absurd/issues/116."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(run_after=None)  # type: ignore[call-overload]
    assert str(excinfo.value) == expected


def test_per_invocation_field_cannot_decorate() -> None:
    expected = (
        "idempotency_key can only be set per invocation, not on a task "
        "definition. Bind it at enqueue time instead:\n\n"
        "    absurd_params(idempotency_key=...).bind(send_report).enqueue(...)"
    )

    def send_report(user_id: int) -> None:
        return None

    with pytest.raises(TypeError) as excinfo:
        absurd_params(idempotency_key="k")(send_report)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_per_invocation_fields_join_and_pick_first_in_message() -> None:
    expected = (
        "headers, idempotency_key can only be set per invocation, not on a task "
        "definition. Bind it at enqueue time instead:\n\n"
        "    absurd_params(headers=...).bind(send_report).enqueue(...)"
    )

    def send_report(user_id: int) -> None:
        return None

    params = absurd_params(headers={}, idempotency_key="k")
    with pytest.raises(TypeError) as excinfo:
        params(send_report)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_params_above_task_are_rejected() -> None:
    expected = (
        "apply @absurd_params below @task, not above it:\n\n"
        "    @task\n    @absurd_params(max_attempts=3)\n"
        "    def send_report(user_id: int) -> None: ..."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(max_attempts=3)(tasks.add)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_bind_rejects_a_non_task() -> None:
    expected = (
        "absurd_params(...).bind() takes a Task, got int. To set defaults on a "
        "task definition, apply absurd_params(...) as a decorator below @task."
    )
    with pytest.raises(TypeError) as excinfo:
        absurd_params(max_attempts=3).bind(3)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_stacked_decorators_merge_the_same_way() -> None:
    strategy: RetryStrategy = {"kind": "none"}

    @absurd_params(max_attempts=4)
    @absurd_params(max_attempts=9, retry_strategy=strategy)
    def stacked(a: int, b: int) -> int:
        return a + b

    attached: t.Any = stacked
    assert attached.absurd_params == {"max_attempts": 4, "retry_strategy": strategy}
