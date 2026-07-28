"""Shared helpers for the admin HTTP test package."""

import importlib
import typing as t

from bs4 import BeautifulSoup, ResultSet, Tag
from django.conf import settings
from django.contrib import admin as djadmin
from django.core.management import call_command
from django.urls import clear_url_caches

from django_absurd import absurd_params
from django_absurd.admin import register_absurd_admin
from tests import tasks
from tests.utils import HasContent

if t.TYPE_CHECKING:
    from django.tasks import TaskResult

BACKEND = "django_absurd.backends.AbsurdBackend"


def parse_html(response: HasContent) -> BeautifulSoup:
    return BeautifulSoup(response.content, "html.parser")


def result_rows(soup: BeautifulSoup) -> ResultSet[Tag]:
    return soup.select("#result_list tbody tr")


def register_admin() -> None:
    register_absurd_admin([djadmin.site])
    importlib.reload(importlib.import_module(settings.ROOT_URLCONF))
    clear_url_caches()


def seed() -> None:
    call_command("absurd_sync_queues")
    tasks.add.enqueue(2, 3)
    tasks.add.using(queue_name="other").enqueue(7, 8)
    tasks.boom.enqueue()
    call_command("absurd_worker", queue="default", burst=True)
    call_command("absurd_worker", queue="other", burst=True)


def seed_mixed() -> tuple[
    "TaskResult[t.Any, t.Any]", "TaskResult[t.Any, t.Any]", "TaskResult[t.Any, t.Any]"
]:
    """Three default-queue tasks in distinct terminal/queued states."""
    call_command("absurd_sync_queues")
    completed = tasks.add.enqueue(2, 3)
    failed = absurd_params(max_attempts=1).bind(tasks.boom).enqueue()
    call_command("absurd_worker", queue="default", burst=True)
    pending = tasks.add.enqueue(5, 6)  # enqueued after the burst → never claimed
    return completed, failed, pending
