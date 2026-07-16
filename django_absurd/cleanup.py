import typing as t

from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import ProgrammingError

from django_absurd.queues import resolve_absurd_database


class QueueCleanup(t.TypedDict):
    queue_name: str
    tasks_deleted: int
    events_deleted: int


def cleanup_all_queues() -> list[QueueCleanup]:
    using = resolve_absurd_database()
    try:
        with connections[using].cursor() as cur:
            cur.execute(
                "select queue_name, tasks_deleted, events_deleted "
                "from absurd.cleanup_all_queues()"
            )
            rows = cur.fetchall()
    except ProgrammingError as exc:
        msg = "Absurd schema is not installed. Run: manage.py migrate"
        raise ImproperlyConfigured(msg) from exc
    return [
        QueueCleanup(queue_name=queue_name, tasks_deleted=tasks, events_deleted=events)
        for queue_name, tasks, events in rows
    ]
