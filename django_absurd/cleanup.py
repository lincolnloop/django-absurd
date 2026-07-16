from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import ProgrammingError

from django_absurd.queues import resolve_absurd_database

CLEANUP_COLUMNS = ("queue_name", "tasks_deleted", "events_deleted")


def cleanup_all_queues() -> list[dict]:
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
    return [dict(zip(CLEANUP_COLUMNS, row, strict=True)) for row in rows]
