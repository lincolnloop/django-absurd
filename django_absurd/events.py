"""Emit an Absurd event from outside a running task (e.g. a Django view)."""

import typing as t

import psycopg.errors
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

if t.TYPE_CHECKING:
    from absurd_sdk import JsonValue


def emit_event(
    event_name: str, payload: "JsonValue | None" = None, *, queue: str = "default"
) -> None:
    from django_absurd.backends import get_declared_queues  # noqa: PLC0415
    from django_absurd.queues import (  # noqa: PLC0415
        get_absurd_backend,
        get_absurd_client,
    )

    backend = get_absurd_backend()
    if backend is None:
        msg = "django-absurd: no Absurd backend configured."
        raise ImproperlyConfigured(msg)
    declared = get_declared_queues(backend)
    if queue not in declared:
        msg = (
            f"Queue '{queue}' is not declared in TASKS QUEUES. "
            "Add it to the QUEUES list in your TASKS backend settings."
        )
        raise ImproperlyConfigured(msg)
    client = get_absurd_client()
    try:
        with transaction.atomic(using=backend.database, savepoint=True):
            client.emit_event(event_name, payload, queue_name=queue)
    except psycopg.errors.UndefinedTable:
        msg = (
            f"Queue '{queue}' is declared but its Absurd table is not provisioned. "
            "Run: manage.py absurd_sync_queues"
        )
        raise ImproperlyConfigured(msg) from None
