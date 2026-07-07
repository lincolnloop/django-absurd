import pytest
from bs4 import BeautifulSoup
from django.core.management import call_command
from django.urls import reverse, reverse_lazy

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_changelist")
ADD = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_add")

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "QUEUES": {"default": {}, "other": {}, "reports": {}},
            "SCHEDULER": "pg_cron",
            "SCHEDULE": {
                "nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
                "hourly": {
                    "task": "tests.tasks.on_reports",
                    "cron": "0 * * * *",
                    "queue": "reports",
                },
            },
        },
    }
}


def seed(settings):
    settings.TASKS = TASKS
    call_command("absurd_sync_queues")
    sync_crons(get_absurd_backends()["default"])


def rows(response):
    soup = BeautifulSoup(response.content, "html.parser")
    return soup.select("#result_list tbody tr")


def change_url(pk):
    return reverse("admin:django_absurd_pg_cron_scheduledtask_change", args=[pk])


def test_changelist_renders_one_row_per_schedule(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    assert len(rows(client.get(CHANGELIST))) == 2


def test_changelist_shows_expected_columns(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    body = client.get(CHANGELIST).content.decode()
    assert "nightly" in body
    assert "0 2 * * *" in body
    assert "tests.tasks.add" in body


def test_queue_filter_renders_and_narrows(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    # list_filter must render a queue filter in the sidebar (a raw URL param would
    # narrow regardless, so assert the filter UI exists — not just the narrowing).
    soup = BeautifulSoup(client.get(CHANGELIST).content, "html.parser")
    assert soup.select_one('#changelist-filter a[href*="queue=reports"]') is not None
    narrowed = rows(client.get(CHANGELIST, {"queue": "reports"}))
    assert len(narrowed) == 1
    assert "hourly" in narrowed[0].get_text()


def test_search_by_name_narrows(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    assert len(rows(client.get(CHANGELIST, {"q": "nightly"}))) == 1


def test_no_add_link_and_add_forbidden(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(CHANGELIST).content, "html.parser")
    assert soup.select_one(".object-tools a.addlink") is None
    assert client.get(ADD).status_code == 403


def test_detail_is_readonly_and_shows_option_columns(settings, client, admin_user):
    seed(settings)
    pk = ScheduledTask.objects.get(name="hourly").pk
    client.force_login(admin_user)
    resp = client.get(change_url(pk))
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.content, "html.parser")
    assert soup.select_one('input[name="cron"]') is None  # read-only, not editable
    assert "reports" in resp.content.decode()  # queue option column rendered
