import typing as t

import pytest
from django.tasks import task

from django_absurd.params import (
    AbsurdDefaultParams,
    AbsurdSpawnParams,
    absurd_default_params,
)
from tests.tasks import add


# Module-level: validate_task rejects a @task with a "<locals>" qualname.
@task
@absurd_default_params(max_attempts=7)
def good_default(a: int, b: int) -> int:
    return a + b


def test_to_kwargs_emits_only_set_fields() -> None:
    assert AbsurdSpawnParams(max_attempts=3).to_kwargs() == {"max_attempts": 3}
    assert AbsurdSpawnParams().to_kwargs() == {}


def test_spawnparams_carries_per_invocation_fields() -> None:
    params = AbsurdSpawnParams(idempotency_key="k", headers={"x": "1"})
    assert params.to_kwargs() == {"idempotency_key": "k", "headers": {"x": "1"}}


def test_decorator_rejects_per_invocation_kwarg() -> None:
    with pytest.raises(TypeError):
        absurd_default_params(idempotency_key="k")


def test_decorator_attaches_default_to_task_func() -> None:
    func_with_params: t.Any = good_default.func
    assert func_with_params.absurd_default_params == AbsurdDefaultParams(max_attempts=7)


def test_decorator_above_task_raises() -> None:
    with pytest.raises(TypeError):
        absurd_default_params(max_attempts=7)(add)  # add is a Task -> wrong order
