import typing as t

from django.db import connections

from django_absurd.queues import resolve_absurd_database


class QueueCleanup(t.TypedDict):
    queue_name: str
    tasks_deleted: int
    events_deleted: int


def cleanup_all_queues() -> list[QueueCleanup]:
    using = resolve_absurd_database()
    with connections[using].cursor() as cur:
        cur.execute(
            "select queue_name, tasks_deleted, events_deleted "
            "from absurd.cleanup_all_queues()"
        )
        rows = cur.fetchall()
    return [
        QueueCleanup(queue_name=queue_name, tasks_deleted=tasks, events_deleted=events)
        for queue_name, tasks, events in rows
    ]
