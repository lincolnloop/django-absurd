"""The Absurd-backend Task subclass and the spawn-option primitives it carries.

Leaf module by design: it imports absurd_sdk types and django.tasks.base.Task and
nothing from django_absurd, so django_absurd.params can import both this module and
django_absurd.backends without a cycle.
"""

import copy
import dataclasses
import logging
import typing as t

from absurd_sdk import CancellationPolicy, JsonObject, RetryStrategy
from django.tasks.base import Task, TaskResult

logger = logging.getLogger(__name__)

WARNED_TASK_PATHS: set[str] = set()


class SpawnKwargs(t.TypedDict, total=False):
    """The exact keyword-arg shape absurd_sdk's ``spawn``/``_normalize_spawn_options``
    accept — mirrors their signatures field-for-field so ``**merged`` calls into them
    type-check per field instead of collapsing to a union."""

    max_attempts: int | None
    # One known gap we deliberately do not guard: an OMITTED ``kind`` raises, though
    # RetryStrategy is total=False — _serialize_retry_strategy indexes
    # strategy["kind"] unconditionally, so {"base_seconds": 5} type-checks and then
    # dies at enqueue with KeyError: 'kind'. No checker can flag that one. An
    # unrecognised kind, or an out-of-range base_seconds/factor/max_seconds, needs no
    # guard from us: absurd.spawn_task validates the strategy up front and raises
    # SQLSTATE AB003, so the enqueue fails instead of quietly retrying with no backoff.
    retry_strategy: RetryStrategy
    cancellation: CancellationPolicy
    headers: JsonObject
    idempotency_key: str


if t.TYPE_CHECKING:
    # Task is Generic[_P, _R] to django-stubs but is a plain dataclass at runtime, where
    # subscripting it raises "type 'Task' is not subscriptable".
    TaskBase = Task[t.Any, t.Any]
else:
    TaskBase = Task


@dataclasses.dataclass(frozen=True, kw_only=True)
class AbsurdTask(TaskBase):
    """A Task that carries its resolved Absurd spawn params.

    The params are a dataclass FIELD rather than a ``__slots__`` entry because
    ``Task.using()`` is ``dataclasses.replace()``, which rebuilds from fields only — a
    slot would be dropped silently by ``bind(task).using(...)``. As a field they also
    survive deepcopy and pickling and participate in __eq__/__repr__.

    Registered as AbsurdBackend.task_class, so a plain ``@task()`` yields one of these
    with no definition-site change.
    """

    absurd_params: SpawnKwargs | None = None

    def enqueue(self, *args: t.Any, **kwargs: t.Any) -> "TaskResult[t.Any, t.Any]":
        warn_if_params_are_inert(self)
        return super().enqueue(*args, **kwargs)

    async def aenqueue(
        self, *args: t.Any, **kwargs: t.Any
    ) -> "TaskResult[t.Any, t.Any]":
        warn_if_params_are_inert(self)
        return await super().aenqueue(*args, **kwargs)

    def __post_init__(self) -> None:
        defaults = getattr(self.func, "absurd_params", None)
        # Fold whenever either layer exists, not just when defaults do: the merge is
        # also what copies nested values away from the caller, and a task built with
        # params but no decorator defaults would otherwise store the caller's dict by
        # reference. Both absent stays None rather than becoming {}.
        if defaults is not None or self.absurd_params is not None:
            object.__setattr__(
                self,
                "absurd_params",
                build_merged_spawn_options(defaults, self.absurd_params),
            )
        super().__post_init__()


def warn_if_params_are_inert(task: AbsurdTask) -> None:
    """Report, once per task, that a spawn about to happen will drop its params.

    Checked at enqueue rather than at ``bind``, because only here is it settled: a task
    bound on the wrong backend and then routed onto Absurd applies its params fine, and
    warning at bind time would cry wolf over it. Identifying the backend by the task
    class it builds, rather than ``isinstance(..., AbsurdBackend)``, keeps this module a
    leaf — importing the backend here would close a cycle through ``backends.py``.
    """
    if task.absurd_params is None:
        return
    if getattr(type(task.get_backend()), "task_class", None) is AbsurdTask:
        return
    if task.module_path in WARNED_TASK_PATHS:
        return
    WARNED_TASK_PATHS.add(task.module_path)
    logger.warning(
        "absurd_params ignored: %s ran on task backend %r, which is not an Absurd "
        "backend",
        task.module_path,
        task.backend,
    )


def build_merged_spawn_options(
    defaults: SpawnKwargs | None, per_call: SpawnKwargs | None
) -> SpawnKwargs:
    """Merge two param layers into a fresh dict, the later one winning per field.

    Nested values are copied, so nothing downstream can mutate a task's stored params
    (or a caller's dict) in place.
    """
    merged: SpawnKwargs = {}
    for layer in (defaults, per_call):
        if layer is not None:
            merged.update(copy.deepcopy(layer))
    return merged
