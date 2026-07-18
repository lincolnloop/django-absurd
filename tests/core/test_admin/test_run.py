import typing as t

import pytest
from django.contrib.admin.utils import quote
from django.contrib.auth.models import AbstractBaseUser, User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse, reverse_lazy

from django_absurd.models import Run
from tests.core.test_admin.support import parse_html, result_rows, seed_mixed
from tests.tasks import add

if t.TYPE_CHECKING:
    from bs4 import Tag
    from django.tasks import TaskResult

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_run_changelist")


def change_url(pk: str) -> str:
    return reverse("admin:django_absurd_run_change", args=[quote(pk)])


run_model: t.Any = Run


def run_for(result: "TaskResult[t.Any, t.Any]") -> t.Any:
    return run_model.objects.get(task_id=result.id.split(":", 1)[1])


def test_changelist_shows_dates_ordered_by_recent_activity(
    client: Client,
    admin_user: AbstractBaseUser,
) -> None:
    call_command("absurd_sync_queues")
    older = add.enqueue(1, 1)
    call_command("absurd_worker", queue="default", burst=True)  # older run starts
    newer = add.enqueue(2, 2)
    call_command("absurd_worker", queue="default", burst=True)  # newer run starts later
    client.force_login(t.cast("User", admin_user))
    response = client.get(CHANGELIST)
    soup = parse_html(response)
    # the started_at column is the (descending) primary sort, and
    # a date column shows
    assert soup.select_one("th.column-started_at.sorted.descending") is not None
    assert soup.select_one(".column-completed_at") is not None
    # rows actually come back most-recently-started first
    keys: list[str] = [
        t.cast("Tag", r.select_one(".field-natural_key")).get_text(strip=True)
        for r in result_rows(soup)
    ]
    newer_key = run_for(newer).natural_key
    older_key = run_for(older).natural_key
    assert keys.index(newer_key) < keys.index(older_key)


def test_changelist_filtered_to_task(
    client: Client,
    admin_user: AbstractBaseUser,
) -> None:
    _, failed, _ = seed_mixed()
    client.force_login(t.cast("User", admin_user))
    # the natural_key is "<queue>:<task_id>";
    # search by the bare task_id
    task_id = failed.id.split(":", 1)[1]
    response = client.get(CHANGELIST, {"q": task_id})
    soup = parse_html(response)
    rows = result_rows(soup)
    # boom runs with max_attempts=1 → exactly one (failed) run for this task
    assert len(rows) == 1
    assert {
        t.cast("Tag", r.select_one(".field-task_id")).get_text(strip=True) for r in rows
    } == {task_id}
    assert {
        t.cast("Tag", r.select_one(".field-state")).get_text(strip=True) for r in rows
    } == {"failed"}


def test_detail_groups_fields_into_fieldsets(
    client: Client,
    admin_user: AbstractBaseUser,
) -> None:
    seed_mixed()  # produces runs
    client.force_login(t.cast("User", admin_user))
    run_obj = run_model.objects.first()
    response = client.get(change_url(run_obj.natural_key))
    soup = parse_html(response)
    legends = {h.get_text(strip=True) for h in soup.select("h2.fieldset-heading")}
    assert {"Claim", "Timing", "Event", "Result"} <= legends


def test_detail_shows_failure_reason(
    client: Client,
    admin_user: AbstractBaseUser,
) -> None:
    seed_mixed()
    client.force_login(t.cast("User", admin_user))
    client.get(CHANGELIST)  # prime the runs view
    run_obj = run_model.objects.filter(queue="default", state="failed").first()
    response = client.get(change_url(run_obj.natural_key))
    soup = parse_html(response)
    failure = soup.select_one(".field-failure_reason .readonly")
    assert failure is not None
    text = failure.get_text()
    assert "boom" in text or "ValueError" in text
