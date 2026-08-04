import logging
import typing as t

from django.db import connections

from django_absurd.queues import resolve_absurd_database

logger = logging.getLogger(__name__)


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
    log_cleanup_result(rows)
    return rows


def log_cleanup_result(rows: list[QueueCleanup]) -> None:
    removed = [r for r in rows if r["tasks_deleted"] or r["events_deleted"]]
    if not removed:
        logger.info("cleanup removed nothing")
        return
    detail = ", ".join(
        f"{r['queue_name']}: tasks={r['tasks_deleted']} events={r['events_deleted']}"
        for r in removed
    )
    logger.info("cleanup removed rows: %s", detail)
