"""Dataclasses and decorator for Absurd spawn/default parameters."""

import dataclasses
import typing as t

from absurd_sdk import CancellationPolicy, JsonObject, RetryStrategy
from django.tasks import Task

NOT_SET: t.Any = object()


@dataclasses.dataclass(frozen=True)
class AbsurdDefaultParams:
    """Per-task default parameters passed to Absurd at enqueue time."""

    max_attempts: int = NOT_SET
    retry_strategy: RetryStrategy = NOT_SET
    cancellation: CancellationPolicy = NOT_SET

    def to_kwargs(self) -> dict[str, t.Any]:
        return {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if getattr(self, f.name) is not NOT_SET
        }


@dataclasses.dataclass(frozen=True)
class AbsurdSpawnParams(AbsurdDefaultParams):
    """Per-invocation parameters passed to Absurd at enqueue time."""

    headers: JsonObject = NOT_SET
    idempotency_key: str = NOT_SET


def absurd_default_params(**kwargs: t.Any) -> t.Callable[[t.Any], t.Any]:
    """Decorator factory that attaches Absurd default params to a task function."""
    params = AbsurdDefaultParams(**kwargs)

    def set_default(func: t.Any) -> t.Any:
        if isinstance(func, Task):
            msg = "apply @absurd_default_params below @task, not above it"
            raise TypeError(msg)
        func.absurd_default_params = params
        return func

    return set_default
