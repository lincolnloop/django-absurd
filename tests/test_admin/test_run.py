import pytest
from django.contrib.admin.utils import quote
from django.core.management import call_command
from django.urls import reverse, reverse_lazy

from django_absurd.models import Run
from tests.tasks import add
from tests.test_admin.support import parse_html, result_rows, seed_mixed

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_run_changelist")


def change_url(pk):
    return reverse("admin:django_absurd_run_change", args=[quote(pk)])


def run_for(result):
    return Run.objects.get(task_id=result.id.split(":", 1)[1])


def test_changelist_shows_dates_ordered_by_recent_activity(client, admin_user):
    call_command("absurd_sync_queues")
    older = add.enqueue(1, 1)
    call_command("absurd_worker", queue="default", burst=True)  # older run starts
    newer = add.enqueue(2, 2)
    call_command("absurd_worker", queue="default", burst=True)  # newer run starts later
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    # the started_at column is the (descending) primary sort, and a date column shows
    assert soup.select_one("th.column-started_at.sorted.descending") is not None
    assert soup.select_one(".column-completed_at") is not None
    # rows actually come back most-recently-started first
    keys = [
        r.select_one(".field-natural_key").get_text(strip=True)
        for r in result_rows(soup)
    ]
    assert keys.index(run_for(newer).natural_key) < keys.index(
        run_for(older).natural_key
    )


def test_changelist_filtered_to_task(client, admin_user):
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    # the natural_key is "<queue>:<task_id>"; search by the bare task_id
    task_id = failed.id.split(":", 1)[1]
    soup = parse_html(client.get(CHANGELIST, {"q": task_id}))
    rows = result_rows(soup)
    # boom runs with max_attempts=1 → exactly one (failed) run for this task
    assert len(rows) == 1
    assert {r.select_one(".field-task_id").get_text(strip=True) for r in rows} == {
        task_id
    }
    assert {r.select_one(".field-state").get_text(strip=True) for r in rows} == {
        "failed"
    }


def test_detail_groups_fields_into_fieldsets(client, admin_user):
    seed_mixed()  # produces runs
    client.force_login(admin_user)
    run = Run.objects.first()
    soup = parse_html(client.get(change_url(run.natural_key)))
    legends = {h.get_text(strip=True) for h in soup.select("h2.fieldset-heading")}
    assert {"Claim", "Timing", "Event", "Result"} <= legends


def test_detail_shows_failure_reason(client, admin_user):
    seed_mixed()
    client.force_login(admin_user)
    client.get(CHANGELIST)  # prime the runs view
    run = Run.objects.filter(queue="default", state="failed").first()
    soup = parse_html(client.get(change_url(run.natural_key)))
    failure = soup.select_one(".field-failure_reason .readonly")
    assert failure is not None
    text = failure.get_text()
    assert "boom" in text or "ValueError" in text
