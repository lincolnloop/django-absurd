import typing as t

from django.db import connections

from django_absurd.queues import resolve_absurd_database


class QueueCleanup(t.TypedDict):
    queue_name: str
    tasks_deleted: int
    events_deleted: int


def cleanup_queues(queues: list[str] | None = None) -> list[QueueCleanup]:
    # A None queue arg to absurd.cleanup_all_queues() cleans every queue in one call;
    # a name cleans that one. Loop over the requested names, or [None] for all.
    targets: list[str | None] = list(queues) if queues is not None else [None]
    using = resolve_absurd_database()
    rows: list[QueueCleanup] = []
    with connections[using].cursor() as cur:
        for target in targets:
            cur.execute(
                "select queue_name, tasks_deleted, events_deleted "
                "from absurd.cleanup_all_queues(%s)",
                [target],
            )
            rows.extend(
                QueueCleanup(
                    queue_name=queue_name, tasks_deleted=tasks, events_deleted=events
                )
                for queue_name, tasks, events in cur.fetchall()
            )
    return rows
