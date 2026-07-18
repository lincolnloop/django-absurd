import typing as t
import uuid

import pytest
from django.contrib.admin.utils import quote
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connections
from django.test import Client, override_settings
from django.urls import reverse, reverse_lazy

from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_admin_model
from django_absurd.queues import get_absurd_client
from tests.atasks import DURABLE_STEP_CALLS, asleep_for_once
from tests.core.test_admin.support import (
    BACKEND,
    parse_html,
    result_rows,
    seed,
    seed_mixed,
)
from tests.tasks import add

if t.TYPE_CHECKING:
    from bs4 import Tag
    from pytest_django.plugin import DjangoDbBlocker

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_task_changelist")
ADD = reverse_lazy("admin:django_absurd_task_add")
INDEX = reverse_lazy("admin:index")


def change_url(pk: str) -> str:
    return reverse("admin:django_absurd_task_change", args=[quote(pk)])


def queue_change_url(pk: str) -> str:
    return reverse("admin:django_absurd_queue_change", args=[quote(pk)])


def find_task(queue: str, task_name: str) -> t.Any:
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    model: t.Any = build_admin_model(spec)
    return model.objects.filter(queue=queue, task_name=task_name).first()


def extract_field_texts(rows: t.Any, field: str) -> set[str]:
    """Extract text from field elements in result rows."""
    return {t.cast("Tag", r.select_one(f".{field}")).get_text(strip=True) for r in rows}


def test_changelist_unions_and_filters(client: Client, admin_user: User) -> None:
    seed()
    client.force_login(admin_user)
    resp = client.get(CHANGELIST)
    soup = parse_html(resp)
    rows = result_rows(soup)
    queues = extract_field_texts(rows, "field-queue")
    names = extract_field_texts(rows, "field-task_name")
    assert queues == {"default", "other"}
    assert names == {"tests.tasks.add", "tests.tasks.boom"}

    sidebar = soup.select_one("#changelist-filter")
    assert sidebar is not None
    assert sidebar.select_one('a[href*="queue=default"]') is not None
    assert sidebar.select_one('a[href*="queue=other"]') is not None

    resp2 = client.get(CHANGELIST, {"queue": "other"})
    fsoup = parse_html(resp2)
    frows = result_rows(fsoup)
    fqueues = extract_field_texts(frows, "field-queue")
    assert fqueues == {"other"}
    fnames = extract_field_texts(frows, "field-task_name")
    assert fnames == {"tests.tasks.add"}  # boom is on the default queue only


def test_changelist_shows_mixed_states(client: Client, admin_user: User) -> None:
    seed_mixed()
    client.force_login(admin_user)
    resp = client.get(CHANGELIST)
    soup = parse_html(resp)
    states = extract_field_texts(result_rows(soup), "field-state")
    assert states == {"pending", "completed", "failed"}


def test_changelist_filters_by_state(client: Client, admin_user: User) -> None:
    seed_mixed()
    client.force_login(admin_user)
    resp_failed = client.get(CHANGELIST, {"state": "failed"})
    failed = parse_html(resp_failed)
    failed_states = extract_field_texts(result_rows(failed), "field-state")
    assert failed_states == {"failed"}
    resp_pending = client.get(CHANGELIST, {"state": "pending"})
    pending = parse_html(resp_pending)
    pending_states = extract_field_texts(result_rows(pending), "field-state")
    assert pending_states == {"pending"}


def test_changelist_search_narrows_by_task_name(
    client: Client, admin_user: User
) -> None:
    seed_mixed()  # two add tasks + one boom
    client.force_login(admin_user)
    resp = client.get(CHANGELIST, {"q": "tests.tasks.boom"})
    soup = parse_html(resp)
    names = extract_field_texts(result_rows(soup), "field-task_name")
    assert names == {"tests.tasks.boom"}


def test_changelist_shows_dates_ordered_by_recent_activity(
    client: Client, admin_user: User
) -> None:
    call_command("absurd_sync_queues")  # index the default queue
    older = add.enqueue(1, 1)
    newer = add.enqueue(2, 2)  # enqueued later → more recent activity
    client.force_login(admin_user)
    resp = client.get(CHANGELIST)
    soup = parse_html(resp)
    rows = result_rows(soup)
    # primary sort is the first_started_at datetime column, descending
    assert soup.select_one("th.column-first_started_at.sorted.descending") is not None
    # rows actually come back most-recent first (order matters, so keep a list)
    keys = [
        el.get_text(strip=True)
        for r in rows
        if (el := r.select_one(".field-natural_key")) is not None
    ]
    assert keys.index(newer.id) < keys.index(older.id)
    # the enqueue_at column renders an actual datetime, not an empty cell
    enqueue_elem = rows[0].select_one(".field-enqueue_at")
    assert enqueue_elem is not None
    assert enqueue_elem.get_text(strip=True) != ""


def test_changelist_warns_about_unindexed_queue(
    client: Client, admin_user: User
) -> None:
    # build the views over the declared queues, then create a queue directly
    # (config drift): it lands in the catalog but no view arm references it →
    # unindexed.
    call_command("absurd_sync_queues")
    get_absurd_client().create_queue("drift")
    client.force_login(admin_user)
    resp = client.get(CHANGELIST)
    soup = parse_html(resp)
    warning = soup.select_one("ul.messagelist li.warning")
    assert warning is not None
    text = warning.get_text()
    assert "drift" in text
    assert "absurd_sync_queues" in text


def test_changelist_no_warning_when_all_queues_indexed(
    client: Client, admin_user: User
) -> None:
    seed_mixed()  # syncs + workers → every catalog queue is an arm
    client.force_login(admin_user)
    resp = client.get(CHANGELIST)
    soup = parse_html(resp)
    assert soup.select_one("ul.messagelist li.warning") is None


def test_changelist_survives_staleness_detection_failure(
    client: Client,
    admin_user: User,
    django_db_blocker: "DjangoDbBlocker",
) -> None:
    client.force_login(admin_user)
    with django_db_blocker.unblock():
        call_command("migrate", "django_absurd", "zero", verbosity=0)
    try:
        resp = client.get(CHANGELIST)
        assert resp.status_code == 200
        # detection failure degrades silently — no spurious staleness warning
        assert parse_html(resp).select_one("ul.messagelist li.warning") is None
    finally:
        with django_db_blocker.unblock():
            call_command("migrate", "django_absurd", verbosity=0)


def test_detail_shows_state(client: Client, admin_user: User) -> None:
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    resp = client.get(change_url(failed.id))
    soup = parse_html(resp)
    state_elem = soup.select_one(".field-state .readonly")
    assert state_elem is not None
    assert state_elem.get_text(strip=True) == "failed"


def test_detail_for_missing_object_does_not_500(
    client: Client, admin_user: User
) -> None:
    seed_mixed()
    client.force_login(admin_user)
    resp = client.get(change_url(f"default:{uuid.uuid4()}"))
    assert resp.status_code in (302, 404)
    resp2 = client.get(queue_change_url("nonexistent"))
    assert resp2.status_code in (302, 404)


def test_detail_groups_fields_and_inlines_runs(
    client: Client, admin_user: User
) -> None:
    completed, _, _ = seed_mixed()  # a completed task → has at least one run
    client.force_login(admin_user)
    resp = client.get(change_url(completed.id))
    soup = parse_html(resp)
    legends = {h.get_text(strip=True) for h in soup.select("h2.fieldset-heading")}
    assert {"State", "Schedule", "Configuration", "Result"} <= legends
    inline = soup.select_one(".inline-group")
    assert inline is not None
    assert inline.select_one(".field-attempt") is not None
    assert inline.select_one(".field-state") is not None
    link = inline.select_one('a[href*="/django_absurd/run/"]')
    assert link is not None
    href = link.get("href")
    assert href is not None
    assert isinstance(href, str)
    assert href.endswith("/change/")


def test_detail_inlines_checkpoints_and_run_available_at(
    client: Client, admin_user: User
) -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    asleep_for_once.enqueue("admin-k")
    call_command("absurd_worker", queue="default", burst=True)  # suspends
    client.force_login(admin_user)

    task = find_task("default", "tests.atasks.asleep_for_once")
    assert task is not None
    soup = parse_html(client.get(change_url(task.natural_key)))

    groups = soup.select(".inline-group")
    assert len(groups) >= 2  # runs + checkpoints
    assert soup.select_one('a[href*="/django_absurd/checkpoint/"]') is not None
    names = {
        cell.get_text(strip=True) for cell in soup.select(".field-checkpoint_name")
    }
    assert "bump" in names
    checkpoint_group = next(
        g for g in groups if g.select_one('a[href*="/django_absurd/checkpoint/"]')
    )
    checkpoint_state = checkpoint_group.select_one(".field-state")
    assert checkpoint_state is not None
    assert checkpoint_state.get_text(strip=True) != ""
    available = soup.select_one(".field-available_at")
    assert available is not None
    assert available.get_text(strip=True) != ""  # sleeping run has a wake time


def test_detail_renders_read_only(client: Client, admin_user: User) -> None:
    seed()
    client.force_login(admin_user)
    client.get(CHANGELIST)  # prime the view
    task = find_task("default", "tests.tasks.add")
    assert task is not None
    resp2 = client.get(change_url(task.natural_key))
    soup = parse_html(resp2)
    readonly = soup.select_one(".field-task_name .readonly")
    assert readonly is not None
    assert readonly.get_text(strip=True) == "tests.tasks.add"
    assert soup.select_one('input[name="task_name"]') is None
    assert soup.select_one('textarea[name="params"]') is None


def test_add_view_forbidden(client: Client, admin_user: User) -> None:
    client.force_login(admin_user)
    resp = client.get(ADD)
    assert resp.status_code in (403, 302)


def test_admin_labels_app_as_absurd(client: Client, admin_user: User) -> None:
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    resp_index = client.get(INDEX)
    index = parse_html(resp_index)
    caption = index.select_one("div.app-django_absurd caption a.section")
    assert caption is not None
    assert caption.get_text(strip=True) == "Absurd"
    resp_change = client.get(change_url(failed.id))
    change = parse_html(resp_change)
    app_crumb = change.select_one('.breadcrumbs a[href="/admin/django_absurd/"]')
    assert app_crumb is not None
    assert app_crumb.get_text(strip=True) == "Absurd"


def test_changelist_degrades_when_view_dropped(
    client: Client, admin_user: User
) -> None:
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS absurd.tasks_view")
    client.force_login(admin_user)
    resp = client.get(CHANGELIST)
    assert resp.status_code == 200
    assert result_rows(parse_html(resp)) == []
    call_command("absurd_sync_queues")


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {
                "QUEUES": {
                    "default": {},
                    "part": {"storage_mode": "partitioned"},
                }
            },
        }
    }
)
def test_partitioned_queue_appears_in_changelist(
    client: Client, admin_user: User
) -> None:
    call_command("absurd_sync_queues")
    add.using(queue_name="part").enqueue(1, 1)
    call_command("absurd_worker", queue="part", burst=True)
    client.force_login(admin_user)
    resp = client.get(CHANGELIST)
    soup = parse_html(resp)
    queues = extract_field_texts(result_rows(soup), "field-queue")
    assert "part" in queues
