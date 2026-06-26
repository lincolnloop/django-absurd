import uuid

import pytest
from django.contrib.admin.utils import quote
from django.core.management import call_command
from django.db import connections
from django.test import override_settings
from django.urls import reverse, reverse_lazy

from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_admin_model
from tests.tasks import add
from tests.test_admin.support import BACKEND, parse_html, result_rows, seed, seed_mixed

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_task_changelist")
ADD = reverse_lazy("admin:django_absurd_task_add")
INDEX = reverse_lazy("admin:index")


def change_url(pk):
    return reverse("admin:django_absurd_task_change", args=[quote(pk)])


def queue_change_url(pk):
    return reverse("admin:django_absurd_queue_change", args=[quote(pk)])


def find_task(queue, task_name):
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    return (
        build_admin_model(spec).objects.filter(queue=queue, task_name=task_name).first()
    )


def test_changelist_unions_and_filters(client, admin_user):
    seed()
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    rows = result_rows(soup)
    queues = {r.select_one(".field-queue").get_text(strip=True) for r in rows}
    names = {r.select_one(".field-task_name").get_text(strip=True) for r in rows}
    assert queues == {"default", "other"}
    assert names == {"tests.tasks.add", "tests.tasks.boom"}

    sidebar = soup.select_one("#changelist-filter")
    assert sidebar is not None
    assert sidebar.select_one('a[href*="queue=default"]') is not None
    assert sidebar.select_one('a[href*="queue=other"]') is not None

    fsoup = parse_html(client.get(CHANGELIST, {"queue": "other"}))
    frows = result_rows(fsoup)
    assert {r.select_one(".field-queue").get_text(strip=True) for r in frows} == {
        "other"
    }
    fnames = {r.select_one(".field-task_name").get_text(strip=True) for r in frows}
    assert fnames == {"tests.tasks.add"}  # boom is on the default queue only


def test_changelist_shows_mixed_states(client, admin_user):
    seed_mixed()
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    states = {
        r.select_one(".field-state").get_text(strip=True) for r in result_rows(soup)
    }
    assert states == {"pending", "completed", "failed"}


def test_changelist_filters_by_state(client, admin_user):
    seed_mixed()
    client.force_login(admin_user)
    failed = parse_html(client.get(CHANGELIST, {"state": "failed"}))
    assert {
        r.select_one(".field-state").get_text(strip=True) for r in result_rows(failed)
    } == {"failed"}
    pending = parse_html(client.get(CHANGELIST, {"state": "pending"}))
    assert {
        r.select_one(".field-state").get_text(strip=True) for r in result_rows(pending)
    } == {"pending"}


def test_changelist_search_narrows_by_task_name(client, admin_user):
    seed_mixed()  # two add tasks + one boom
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST, {"q": "tests.tasks.boom"}))
    names = {
        r.select_one(".field-task_name").get_text(strip=True) for r in result_rows(soup)
    }
    assert names == {"tests.tasks.boom"}


def test_changelist_shows_dates_ordered_by_recent_activity(client, admin_user):
    call_command("absurd_sync_queues")  # index the default queue
    older = add.enqueue(1, 1)
    newer = add.enqueue(2, 2)  # enqueued later → more recent activity
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    rows = result_rows(soup)
    # primary sort is the first_started_at datetime column, descending
    assert soup.select_one("th.column-first_started_at.sorted.descending") is not None
    # rows actually come back most-recent first
    keys = [r.select_one(".field-natural_key").get_text(strip=True) for r in rows]
    assert keys.index(newer.id) < keys.index(older.id)
    # the enqueue_at column renders an actual datetime, not an empty cell
    assert rows[0].select_one(".field-enqueue_at").get_text(strip=True) != ""


def test_changelist_warns_about_unindexed_queue(client, admin_user):
    # enqueue auto-creates 'other' (catalog + physical tables) but does NOT rebuild
    # the views, so 'other' is absent from the union view's arms.
    add.using(queue_name="other").enqueue(7, 8)
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    warning = soup.select_one("ul.messagelist li.warning")
    assert warning is not None
    text = warning.get_text()
    assert "other" in text
    assert "absurd_sync_queues" in text


def test_changelist_no_warning_when_all_queues_indexed(client, admin_user):
    seed_mixed()  # syncs + workers → every catalog queue is an arm
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    assert soup.select_one("ul.messagelist li.warning") is None


def test_changelist_survives_staleness_detection_failure(
    client, admin_user, django_db_blocker
):
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


def test_detail_shows_state(client, admin_user):
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    soup = parse_html(client.get(change_url(failed.id)))
    assert soup.select_one(".field-state .readonly").get_text(strip=True) == "failed"


def test_detail_for_missing_object_does_not_500(client, admin_user):
    seed_mixed()
    client.force_login(admin_user)
    resp = client.get(change_url(f"default:{uuid.uuid4()}"))
    assert resp.status_code in (302, 404)
    assert client.get(queue_change_url("nonexistent")).status_code in (302, 404)


def test_detail_groups_fields_and_inlines_runs(client, admin_user):
    completed, _, _ = seed_mixed()  # a completed task → has at least one run
    client.force_login(admin_user)
    soup = parse_html(client.get(change_url(completed.id)))
    legends = {h.get_text(strip=True) for h in soup.select("h2.fieldset-heading")}
    assert {"State", "Schedule", "Configuration", "Result"} <= legends
    inline = soup.select_one(".inline-group")
    assert inline is not None
    assert inline.select_one(".field-attempt") is not None
    assert inline.select_one(".field-state") is not None
    link = inline.select_one('a[href*="/django_absurd/run/"]')
    assert link is not None
    assert link["href"].endswith("/change/")


def test_detail_renders_read_only(client, admin_user):
    seed()
    client.force_login(admin_user)
    client.get(CHANGELIST)  # prime the view
    task = find_task("default", "tests.tasks.add")
    soup = parse_html(client.get(change_url(task.natural_key)))
    readonly = soup.select_one(".field-task_name .readonly")
    assert readonly.get_text(strip=True) == "tests.tasks.add"
    assert soup.select_one('input[name="task_name"]') is None
    assert soup.select_one('textarea[name="params"]') is None


def test_add_view_forbidden(client, admin_user):
    client.force_login(admin_user)
    assert client.get(ADD).status_code in (403, 302)


def test_admin_labels_app_as_absurd(client, admin_user):
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    index = parse_html(client.get(INDEX))
    caption = index.select_one("div.app-django_absurd caption a.section")
    assert caption.get_text(strip=True) == "Absurd"
    change = parse_html(client.get(change_url(failed.id)))
    app_crumb = change.select_one('.breadcrumbs a[href="/admin/django_absurd/"]')
    assert app_crumb.get_text(strip=True) == "Absurd"


def test_changelist_degrades_when_view_dropped(client, admin_user):
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
                "QUEUES": {"default": {}, "part": {"storage_mode": "partitioned"}}
            },
        }
    }
)
def test_partitioned_queue_appears_in_changelist(client, admin_user):
    call_command("absurd_sync_queues")
    add.using(queue_name="part").enqueue(1, 1)
    call_command("absurd_worker", queue="part", burst=True)
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    queues = {
        r.select_one(".field-queue").get_text(strip=True) for r in result_rows(soup)
    }
    assert "part" in queues
