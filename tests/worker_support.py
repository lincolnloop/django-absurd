import typing as t

import psycopg
from absurd_sdk import Absurd, TaskResultSnapshot
from django.core.management import call_command
from django.db import connections

from django_absurd.connection import register_jsonb_loader


def run_absurd_worker(queue: str = "default", concurrency: int = 1) -> None:
    call_command("absurd_worker", queue=queue, burst=True, concurrency=concurrency)


def get_task_result(
    task_id: t.Any, queue: str = "default"
) -> TaskResultSnapshot | None:
    raw_task_id = str(task_id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        return Absurd(conn).fetch_task_result(raw_task_id, queue)
    finally:
        conn.close()
