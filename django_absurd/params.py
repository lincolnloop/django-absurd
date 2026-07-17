"""Dataclasses and decorator for Absurd spawn/default parameters."""

import dataclasses
import enum
import typing as t

from absurd_sdk import CancellationPolicy, JsonObject, RetryStrategy
from django.tasks import Task


class NotSet(enum.Enum):
    """Sentinel type for an unset field — distinct from None, which is a real value."""

    TOKEN = enum.auto()


NOT_SET = NotSet.TOKEN

F = t.TypeVar("F")

AbsurdFieldValue = int | RetryStrategy | CancellationPolicy | JsonObject | str


@dataclasses.dataclass(frozen=True)
class AbsurdDefaultParams:
    """Per-task default parameters passed to Absurd at enqueue time."""

    max_attempts: int | NotSet = NOT_SET
    retry_strategy: RetryStrategy | NotSet = NOT_SET
    cancellation: CancellationPolicy | NotSet = NOT_SET

    def to_kwargs(self) -> dict[str, AbsurdFieldValue]:
        return {
            f.name: value
            for f in dataclasses.fields(self)
            if (value := getattr(self, f.name)) is not NOT_SET
        }


@dataclasses.dataclass(frozen=True)
class AbsurdSpawnParams(AbsurdDefaultParams):
    """Per-invocation parameters passed to Absurd at enqueue time."""

    headers: JsonObject | NotSet = NOT_SET
    idempotency_key: str | NotSet = NOT_SET


def absurd_default_params(
    **kwargs: AbsurdFieldValue,
) -> t.Callable[[F], F]:
    """Decorator factory that attaches Absurd default params to a task function."""
    params = AbsurdDefaultParams(**kwargs)  # type: ignore[arg-type]  # kwargs match dataclass fields

    def set_default(func: F) -> F:
        if isinstance(func, Task):
            msg = "apply @absurd_default_params below @task, not above it"
            raise TypeError(msg)
        func.absurd_default_params = params  # type: ignore[attr-defined]  # dynamic attribute on decorated callable
        return func

    return set_default
