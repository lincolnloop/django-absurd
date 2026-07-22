"""Shared helpers for the pg_cron test suite (plain functions — fixtures live in
conftest.py; pg_cron catalog queries live on ``ScheduledTask.pg_cron``)."""

import typing as t

from tests.utils import make_tasks_settings


def build_pg_cron_tasks(
    schedule: dict[str, dict[str, object]],
) -> dict[str, dict[str, t.Any]]:
    tasks = make_tasks_settings(schedule=schedule)
    tasks["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True
    return tasks
