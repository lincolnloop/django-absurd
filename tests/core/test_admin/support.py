"""Shared helpers for the admin HTTP test package."""

import importlib

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib import admin as djadmin
from django.core.management import call_command
from django.urls import clear_url_caches

from django_absurd.admin import register_absurd_admin
from django_absurd.params import AbsurdSpawnParams
from tests.tasks import add, boom

BACKEND = "django_absurd.backends.AbsurdBackend"


def parse_html(response):
    return BeautifulSoup(response.content, "html.parser")


def result_rows(soup):
    return soup.select("#result_list tbody tr")


def register_admin():
    register_absurd_admin([djadmin.site])
    importlib.reload(importlib.import_module(settings.ROOT_URLCONF))
    clear_url_caches()


def seed():
    call_command("absurd_sync_queues")
    add.enqueue(2, 3)
    add.using(queue_name="other").enqueue(7, 8)
    boom.enqueue()
    call_command("absurd_worker", queue="default", burst=True)
    call_command("absurd_worker", queue="other", burst=True)


def seed_mixed():
    """Three default-queue tasks in distinct terminal/queued states."""
    call_command("absurd_sync_queues")
    completed = add.enqueue(2, 3)
    failed = boom.enqueue(absurd_spawn_params=AbsurdSpawnParams(max_attempts=1))
    call_command("absurd_worker", queue="default", burst=True)
    pending = add.enqueue(5, 6)  # enqueued after the burst → never claimed
    return completed, failed, pending
