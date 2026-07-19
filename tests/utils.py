import uuid

import psycopg
from absurd_sdk import Absurd, TaskResultSnapshot
from django.core.management import call_command
from django.db import connections

from django_absurd.connection import register_jsonb_loader


def run_absurd_worker(queue: str = "default", concurrency: int = 1) -> None:
    call_command("absurd_worker", queue=queue, burst=True, concurrency=concurrency)


def get_task_result(
    task_id: str | uuid.UUID, queue: str = "default"
) -> TaskResultSnapshot | None:
    # task_id is either a TaskResult.id ("queue:uuid" string) or a raw SpawnResult
    # ["task_id"] — the latter is typed str by absurd_sdk's stub, but psycopg
    # deserializes the uuid column to a real uuid.UUID at runtime.
    raw_task_id = str(task_id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        return Absurd(conn).fetch_task_result(raw_task_id, queue)
    finally:
        conn.close()
