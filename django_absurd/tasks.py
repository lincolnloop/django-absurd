"""The Absurd-backend Task subclass and the spawn-option primitives it carries.

Leaf module by design: it imports absurd_sdk types and django.tasks.base.Task and
nothing from django_absurd, so django_absurd.params can import both this module and
django_absurd.backends without a cycle.
"""

import copy
import dataclasses
import typing as t

from absurd_sdk import CancellationPolicy, JsonObject, RetryStrategy
from django.tasks.base import Task


class SpawnKwargs(t.TypedDict, total=False):
    """The exact keyword-arg shape absurd_sdk's ``spawn``/``_normalize_spawn_options``
    accept — mirrors their signatures field-for-field so ``**merged`` calls into them
    type-check per field instead of collapsing to a union."""

    max_attempts: int
    # Two known gaps we deliberately do not guard. An unrecognised ``kind`` is
    # validated NOWHERE: absurd.fail_run does coalesce(v_retry_strategy->>'kind',
    # 'none') and falls through to v_delay_seconds := 0, so a typo retries with no
    # backoff instead of erroring (type checkers catch it — the SDK types kind as a
    # Literal). And an OMITTED ``kind`` raises, though RetryStrategy is total=False:
    # _serialize_retry_strategy indexes strategy["kind"] unconditionally, so
    # {"base_seconds": 5} type-checks and then dies at enqueue with KeyError: 'kind'.
    # No checker can flag that one.
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
class AbsurdTask(TaskBase):  # type: ignore[misc]  # stub declares Task non-frozen
    """A Task that carries its resolved Absurd spawn params.

    The params are a dataclass FIELD rather than a ``__slots__`` entry because
    ``Task.using()`` is ``dataclasses.replace()``, which rebuilds from fields only — a
    slot would be dropped silently by ``bind(task).using(...)``. As a field they also
    survive deepcopy and pickling and participate in __eq__/__repr__.

    Registered as AbsurdBackend.task_class, so a plain ``@task()`` yields one of these
    with no definition-site change.
    """

    absurd_params: SpawnKwargs | None = None

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
