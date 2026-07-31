"""Emit an Absurd event from outside a running task (e.g. a Django view)."""

import typing as t

import psycopg.errors
from django.db import transaction

from django_absurd.exceptions import (
    BackendNotConfiguredError,
    QueueNotDeclaredError,
    QueueNotProvisionedError,
)
from django_absurd.queues import (
    get_absurd_backend,
    get_absurd_client,
    names_a_queue_table,
)

if t.TYPE_CHECKING:
    from absurd_sdk import JsonValue


def emit_event(
    event_name: str, payload: "JsonValue | None" = None, *, queue: str = "default"
) -> None:
    backend = get_absurd_backend()
    if backend is None:
        raise BackendNotConfiguredError(0)
    if queue not in backend.queues:
        raise QueueNotDeclaredError(queue, backend.alias, backend.queues)
    client = get_absurd_client()
    try:
        with transaction.atomic(using=backend.database, savepoint=True):
            client.emit_event(event_name, payload, queue_name=queue)
    except psycopg.errors.UndefinedTable as exc:
        if not names_a_queue_table(exc, queue):
            raise
        raise QueueNotProvisionedError(queue) from exc
