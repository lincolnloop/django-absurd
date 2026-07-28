"""The absurd_params factory: a task decorator and a per-invocation enqueue channel."""

import dataclasses
import enum
import logging
import typing as t

from absurd_sdk import CancellationPolicy, JsonObject, RetryStrategy
from django.tasks import Task

from django_absurd.backends import AbsurdBackend
from django_absurd.tasks import AbsurdTask, SpawnKwargs, build_merged_spawn_options

logger = logging.getLogger("django_absurd")


class NotSet(enum.Enum):
    """Sentinel type for an unset field — distinct from None, which is a real value."""

    TOKEN = enum.auto()


NOT_SET = NotSet.TOKEN

P = t.ParamSpec("P")
R = t.TypeVar("R")


@dataclasses.dataclass(frozen=True)
class ParamsBase:
    """Absurd spawn params waiting for a call site to attach them to."""

    kwargs: SpawnKwargs

    def bind(self, target: "Task[P, R]") -> "Task[P, R]":
        """Attach these params to ``target``, or raise/no-op if it can't carry them."""
        if not isinstance(target, Task):
            msg = (
                f"absurd_params(...).bind() takes a Task, got "
                f"{type(target).__name__}. To set defaults on a task definition, "
                "apply absurd_params(...) as a decorator below @task."
            )
            raise TypeError(msg)
        if not isinstance(target.get_backend(), AbsurdBackend):
            warn_off_backend_bind(target)
            return target
        # Rebuilt from Task's own fields rather than replace(): a task defined on
        # another backend and routed in with .using(backend=...) is a plain Task,
        # so there is no absurd_params field to replace.
        return AbsurdTask(
            **{f.name: getattr(target, f.name) for f in dataclasses.fields(Task)},
            absurd_params=build_merged_spawn_options(
                target.absurd_params if isinstance(target, AbsurdTask) else None,
                self.kwargs,
            ),
        )


class AbsurdParams(ParamsBase):
    """Params legal at a task definition, so this form doubles as a decorator."""

    def __call__(self, target: t.Callable[P, R]) -> t.Callable[P, R]:
        if isinstance(target, Task):
            msg = (
                "apply @absurd_params below @task, not above it:\n\n"
                "    @task\n    @absurd_params(max_attempts=3)\n"
                "    def send_report(user_id: int) -> None: ..."
            )
            raise TypeError(msg)
        target.absurd_params = build_merged_spawn_options(  # type: ignore[attr-defined]  # dynamic attribute on decorated callable
            getattr(target, "absurd_params", None), self.kwargs
        )
        return target


class PerCallParams(ParamsBase):
    """Params carrying a per-invocation field, so only ``bind`` is legal.

    A sibling of AbsurdParams rather than a subclass: ``__call__`` returns NoReturn,
    the bottom type, which no subclass could widen back to a real return value. The
    method exists only so the decorator mistake reaches a curated message; ``t.Never``
    makes every call a static error for users who type-check.
    """

    def __call__(self, target: t.Never) -> t.NoReturn:
        present = sorted(set(self.kwargs) & set(PER_INVOCATION_FIELD_NAMES))
        name = getattr(target, "name", getattr(target, "__name__", repr(target)))
        msg = (
            f"{', '.join(present)} can only be set per invocation, not on a task "
            "definition. Bind it at enqueue time instead:\n\n"
            f"    absurd_params({present[0]}=...).bind({name}).enqueue(...)"
        )
        raise TypeError(msg)


@t.overload
def absurd_params(
    *,
    max_attempts: int = ...,
    retry_strategy: RetryStrategy = ...,
    cancellation: CancellationPolicy = ...,
) -> AbsurdParams: ...


@t.overload
def absurd_params(
    *,
    max_attempts: int = ...,
    retry_strategy: RetryStrategy = ...,
    cancellation: CancellationPolicy = ...,
    headers: JsonObject = ...,
    idempotency_key: str = ...,
) -> PerCallParams: ...


def absurd_params(
    *,
    max_attempts: int | NotSet = NOT_SET,
    retry_strategy: RetryStrategy | NotSet = NOT_SET,
    cancellation: CancellationPolicy | NotSet = NOT_SET,
    headers: JsonObject | NotSet = NOT_SET,
    idempotency_key: str | NotSet = NOT_SET,
    **unsupported: object,
) -> AbsurdParams | PerCallParams:
    """Build Absurd spawn params for a task definition or a single enqueue.

    Every field is optional and an unset one never reaches the payload. The five
    keyword-only params are spelled out so ``inspect.signature`` stays informative
    while anything else lands in ``unsupported`` and gets a curated message rather
    than Python's bare TypeError.
    """
    if unsupported:
        msg = build_unsupported_keyword_message(next(iter(unsupported)))
        raise TypeError(msg)
    values: dict[str, object] = {
        "max_attempts": max_attempts,
        "retry_strategy": retry_strategy,
        "cancellation": cancellation,
        "headers": headers,
        "idempotency_key": idempotency_key,
    }
    kwargs = t.cast(
        "SpawnKwargs", {k: v for k, v in values.items() if v is not NOT_SET}
    )
    if headers is not NOT_SET or idempotency_key is not NOT_SET:
        return PerCallParams(kwargs)
    return AbsurdParams(kwargs)


PER_INVOCATION_FIELD_NAMES = ("headers", "idempotency_key")

FIELD_NAMES = (
    "max_attempts",
    "retry_strategy",
    "cancellation",
    *PER_INVOCATION_FIELD_NAMES,
)

ROUTING_KEYWORDS = ("queue", "queue_name")

WARNED_TASK_PATHS: set[str] = set()


def build_unsupported_keyword_message(name: str) -> str:
    """Explain why a keyword outside the five supported fields was refused."""
    if name in ROUTING_KEYWORDS:
        return (
            f"{name!r} cannot be set through absurd_params — queue routing belongs "
            'to Django\'s Task API:\n\n    send_report.using(queue_name="reports")'
        )
    return (
        f"{name!r} is an invalid argument for absurd_params. Valid arguments: "
        f"{', '.join(FIELD_NAMES)}."
    )


def warn_off_backend_bind(target: "Task[t.Any, t.Any]") -> None:
    """Report, once per task, that binding params to a foreign backend did nothing."""
    if target.module_path in WARNED_TASK_PATHS:
        return
    WARNED_TASK_PATHS.add(target.module_path)
    logger.warning(
        "absurd_params ignored: %s is on task backend %r, which is not an Absurd "
        "backend",
        target.module_path,
        target.backend,
    )
