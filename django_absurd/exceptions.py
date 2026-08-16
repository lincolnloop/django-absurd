import typing as t

QUEUE_READONLY_MSG = (
    "Queue is read-only; manage queues via the AbsurdBackend QUEUES option + "
    "'manage.py absurd_sync_queues', or the absurd-sdk."
)

ADMIN_VIEW_READONLY_MSG = (
    "Absurd queue-table models are read-only; they map Absurd's queue tables."
)


class DjangoAbsurdError(Exception):
    """Base for every typed error this package raises.

    Named for the distributing package, not ``AbsurdError`` — modules import from
    both ``absurd_sdk`` and ``django_absurd``, and the short name reads as the SDK's
    (whose own exceptions share no base). Deliberately not also a
    ``ValueError``/``ImproperlyConfigured`` subclass: alpha status, and the class
    name carries the condition. ``except DjangoAbsurdError`` covers only this
    module's typed errors — plain ``ImproperlyConfigured``/``RuntimeError``/
    ``TypeError`` raised elsewhere in the package are unaffected.
    """


class QueueReadOnlyError(DjangoAbsurdError):
    pass


class ViewNotProvisionedError(DjangoAbsurdError):
    pass


class QueueNotDeclaredError(DjangoAbsurdError):
    """A queue name doesn't match any queue declared for the backend.

    Raised unconditionally by ``worker.drain_queue`` and ``events.emit_event`` for any
    undeclared queue name. ``AbsurdBackend.enqueue`` raises it too, but only when the
    backend's ``QUEUES`` option is empty/unset; with ``QUEUES`` configured, an
    undeclared queue name at enqueue is rejected earlier as Django's own
    ``InvalidTask``, from ``validate_task``.
    """

    def __init__(self, queue: str, alias: str, valid_queues: t.Iterable[str]) -> None:
        sorted_queues = sorted(valid_queues)
        valid = ", ".join(sorted_queues) if sorted_queues else "(none)"
        msg = (
            f"Queue '{queue}' is not declared for backend '{alias}'. "
            f"Valid queues: {valid}. "
            "Add it to the QUEUES list in your TASKS backend settings."
        )
        super().__init__(msg)


class QueueNotProvisionedError(DjangoAbsurdError):
    """A queue is declared but its Absurd table has not been provisioned.

    Raised by ``worker.drain_queue`` and ``events.emit_event`` when a claim/emit hits
    a missing relation that names one of the queue's own Absurd tables.
    """

    def __init__(self, queue: str) -> None:
        msg = (
            f"Queue '{queue}' is declared but its Absurd table is not provisioned. "
            "Run: manage.py absurd_sync_queues"
        )
        super().__init__(msg)


class TaskIdQueueMismatchError(DjangoAbsurdError):
    """An explicit ``queue=`` disagrees with the queue prefix inside a
    ``"queue:uuid"`` task id — ambiguous, so ``AbsurdTestRuntime.get_result`` raises
    rather than silently picking a side.
    """

    def __init__(self, task_id: str, prefix_queue: str, queue: str) -> None:
        msg = (
            f"get_result(): task id '{task_id}' names queue '{prefix_queue}', but "
            f"queue='{queue}' was also passed and disagrees. Pass only one, or make "
            "them agree."
        )
        super().__init__(msg)


class TaskNotFoundError(DjangoAbsurdError):
    """``AbsurdTestRuntime.get_result`` found no task by that id on that queue.

    Raised in place of a ``None`` return so a typo'd id, or a bare uuid resolved
    against the wrong queue, names both instead of surfacing as an
    ``AttributeError`` on a ``None`` read.
    """

    def __init__(self, task_id: str, queue: str) -> None:
        msg = (
            f"No task '{task_id}' found on queue '{queue}'. A bare uuid resolves "
            "to queue 'default'; pass queue=... if the task ran on another queue."
        )
        super().__init__(msg)


class SchemaNotInstalledError(DjangoAbsurdError):
    """The Absurd Postgres schema is not installed on the target database.

    Raised by queue reconcile, the enqueue path, the worker's client probe,
    cleanup, and flush whenever a query hits a missing Absurd relation.
    ``migrate``'s ``post_migrate`` hook is what provisions declared queues, so
    the message names ``migrate`` alone — not ``absurd_sync_queues`` after it.

    Each raising call site classifies with its own psycopg exception tuple, not a
    shared one — the tuple is chosen for what that site's own statement can
    actually hit, so the tuples differing from each other is by design, not drift.
    """

    def __init__(self) -> None:
        msg = "Absurd schema is not installed. Run: manage.py migrate"
        super().__init__(msg)


class BackendNotConfiguredError(DjangoAbsurdError):
    """No single Absurd backend could be resolved — zero or several configured. One
    type for both counts: the package supports exactly one Absurd backend per
    project, so either way there is no single backend to act on.
    """

    def __init__(self, backend_count: int) -> None:
        if backend_count == 0:
            msg = (
                "No Absurd backend configured. Add a "
                "django_absurd.backends.AbsurdBackend entry to TASKS."
            )
        else:
            msg = (
                "django-absurd supports one Absurd backend per project; "
                "configure exactly one AbsurdBackend in TASKS."
            )
        super().__init__(msg)
