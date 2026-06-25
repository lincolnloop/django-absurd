QUEUE_READONLY_MSG = (
    "Queue is read-only; manage queues via the AbsurdBackend QUEUES option + "
    "'manage.py absurd_sync_queues', or the absurd-sdk."
)

ADMIN_VIEW_READONLY_MSG = (
    "Admin view models are read-only; they map Absurd union views."
)


class QueueReadOnlyError(Exception):
    pass


class ViewNotProvisionedError(Exception):
    pass
