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

    Named for the distributing package, deliberately not ``AbsurdError``: the
    upstream ``absurd_sdk`` is the confusing name to anchor on here, since
    ``worker.py`` imports from both it and ``django_absurd``, and the SDK's own
    exceptions (``SuspendTask``, ``CancelledTask``, ``FailedTask``, plus a
    ``TimeoutError`` that shadows the builtin) share no base of their own.

    Deliberately not also a ``ValueError``/``ImproperlyConfigured`` subclass — alpha
    status means no external compatibility to preserve, and the class name already
    carries the condition (``QueueNotDeclaredError`` over a bare ``ValueError``).

    Covers only the exceptions defined in this module — ``except DjangoAbsurdError``
    is "anything this package raised as a typed error," not every error the package
    can raise. Plenty of call sites still raise a plain
    ``ImproperlyConfigured``/``RuntimeError``/``TypeError`` directly and are
    unaffected by this base.
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
    """A ``"queue:uuid"`` task id's own queue prefix disagrees with an explicit
    ``queue=`` argument passed alongside it.

    Raised by ``AbsurdTestRuntime.get_result`` when ``task_id`` carries a queue prefix
    (as Django's own ``TaskResult.id`` always does) and the caller ALSO passed a
    ``queue=`` naming a different queue — an ambiguous request, not one this package
    will silently resolve by picking either side.
    """

    def __init__(self, task_id: str, prefix_queue: str, queue: str) -> None:
        msg = (
            f"get_result(): task id '{task_id}' names queue '{prefix_queue}', but "
            f"queue='{queue}' was also passed and disagrees. Pass only one, or make "
            "them agree."
        )
        super().__init__(msg)


class BackendNotConfiguredError(DjangoAbsurdError):
    """No single Absurd backend could be resolved.

    Raised by ``management.base.resolve_backend`` on zero or on several configured
    backends, and by ``events.emit_event`` on the same zero-backends condition.
    One type for both counts — this package supports exactly one Absurd backend per
    project, so "zero" and "several" both boil down to the same fact for the caller:
    there is no single backend to act on.
    """

    def __init__(self, backend_count: int) -> None:
        if backend_count == 0:
            msg = "No Absurd backend configured."
        else:
            msg = (
                "django-absurd supports one Absurd backend per project; "
                "configure exactly one AbsurdBackend in TASKS."
            )
        super().__init__(msg)
