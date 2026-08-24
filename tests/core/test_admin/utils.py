"""Shared helpers for the admin HTTP test package."""

import importlib
import typing as t
import urllib.parse

from bs4 import BeautifulSoup, ResultSet, Tag
from django.conf import settings
from django.contrib import admin as djadmin
from django.urls import clear_url_caches

from django_absurd import absurd_params, worker
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
    tasks.add.enqueue(2, 3)
    tasks.add.using(queue_name="other").enqueue(7, 8)
    tasks.boom.enqueue()
    worker.drain_queue("default")
    worker.drain_queue("other")


def seed_mixed() -> tuple[
    "TaskResult[t.Any, t.Any]", "TaskResult[t.Any, t.Any]", "TaskResult[t.Any, t.Any]"
]:
    """Three default-queue tasks in distinct terminal/queued states."""
    completed = tasks.add.enqueue(2, 3)
    failed = absurd_params(max_attempts=1).bind(tasks.boom).enqueue()
    worker.drain_queue("default")
    pending = tasks.add.enqueue(5, 6)  # enqueued after the drain → never claimed
    return completed, failed, pending


def extract_filter_choices(soup: BeautifulSoup, param: str) -> set[str]:
    """Labels the changelist sidebar offers for one filter parameter.

    Keyed off the querystring rather than the link text so the "All" entry drops
    out without matching a translated string.
    """
    links = soup.select(f'#changelist-filter details[data-filter-title="{param}"] a')
    return {
        a.get_text(strip=True)
        for a in links
        if names_param(t.cast("str", a.get("href", "")), param)
    }


def names_param(href: str, param: str) -> bool:
    keys = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    return any(k == param or k.startswith(f"{param}__") for k in keys)
