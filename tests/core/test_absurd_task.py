import dataclasses

import pytest
from django.tasks import Task

from django_absurd.tasks import AbsurdTask
from tests import tasks

FULLY_SPECCED_PARAMS = {
    "max_attempts": 9,
    "retry_strategy": {"kind": "fixed", "base_seconds": 5},
    "cancellation": {"max_duration": 45, "max_delay": 3},
}


def test_absurd_backend_builds_absurd_tasks() -> None:
    assert isinstance(tasks.add, AbsurdTask)
    assert isinstance(tasks.add, Task)
    assert tasks.add.absurd_params is None


def test_decorator_defaults_fold_into_the_field() -> None:
    assert isinstance(tasks.fully_specced, AbsurdTask)
    assert tasks.fully_specced.absurd_params == FULLY_SPECCED_PARAMS


def test_using_preserves_the_field() -> None:
    routed = tasks.fully_specced.using(queue_name="other")
    assert isinstance(routed, AbsurdTask)
    assert routed.absurd_params == FULLY_SPECCED_PARAMS


def test_a_per_call_value_wins_per_field_over_the_defaults() -> None:
    assert isinstance(tasks.fully_specced, AbsurdTask)
    bound = dataclasses.replace(tasks.fully_specced, absurd_params={"max_attempts": 1})
    assert isinstance(bound, AbsurdTask)
    assert bound.absurd_params == {**FULLY_SPECCED_PARAMS, "max_attempts": 1}


def test_using_does_not_clobber_a_per_call_value() -> None:
    assert isinstance(tasks.fully_specced, AbsurdTask)
    bound = dataclasses.replace(tasks.fully_specced, absurd_params={"max_attempts": 1})
    replaced = bound.using(queue_name="other")
    assert isinstance(replaced, AbsurdTask)
    assert replaced.absurd_params == {**FULLY_SPECCED_PARAMS, "max_attempts": 1}


def test_the_field_is_read_only() -> None:
    assert isinstance(tasks.add, AbsurdTask)
    with pytest.raises(
        dataclasses.FrozenInstanceError,
        match="cannot assign to field 'absurd_params'",
    ):
        tasks.add.absurd_params = {"max_attempts": 1}  # type: ignore[misc]
