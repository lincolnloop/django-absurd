import importlib
import uuid

import pytest
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib import admin as djadmin
from django.contrib.admin.utils import quote
from django.core.management import call_command
from django.db import connections
from django.test import override_settings
from django.urls import clear_url_caches, reverse

from django_absurd.admin import (
    autoregister_admin,
    register_absurd_admin,
    resolve_admin_sites,
)
from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_admin_model
from django_absurd.params import AbsurdSpawnParams
from tests.tasks import add, boom

BACKEND = "django_absurd.backends.AbsurdBackend"

pytestmark = pytest.mark.django_db(transaction=True)


def parse_html(response):
    return BeautifulSoup(response.content, "html.parser")


def result_rows(soup):
    return soup.select("#result_list tbody tr")


def test_admin_login_page_renders(client):
    resp = client.get("/admin/login/")
    assert resp.status_code == 200


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


def refresh_url_resolver() -> None:
    importlib.reload(importlib.import_module(settings.ROOT_URLCONF))
    clear_url_caches()


def test_six_entries_registered_on_default_site():
    register_absurd_admin([djadmin.site])
    registered = {m._meta.model_name for m in djadmin.site._registry}
    assert {
        "task",
        "run",
        "checkpoint",
        "event",
        "wait",
        "queue",
    } <= registered


def test_staff_user_sees_entries_in_index(client, staff_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    client.force_login(staff_user)
    soup = parse_html(client.get("/admin/"))
    assert soup.select_one('a[href$="/django_absurd/task/"]') is not None


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("tests.admin.custom_site",)},
        }
    }
)
def test_custom_site_registration():
    from tests.admin import custom_site  # noqa: PLC0415

    register_absurd_admin(resolve_admin_sites())
    assert any(m._meta.model_name == "task" for m in custom_site._registry)


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ADMIN_SITE": ("nonexistent.module.site",)},
        }
    }
)
def test_bad_admin_site_fails_soft():
    assert resolve_admin_sites() == []


def test_tasks_changelist_unions_and_filters(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed()
    client.force_login(admin_user)
    url = reverse("admin:django_absurd_task_changelist")

    soup = parse_html(client.get(url))
    rows = result_rows(soup)
    queues = {r.select_one(".field-queue").get_text(strip=True) for r in rows}
    names = {r.select_one(".field-task_name").get_text(strip=True) for r in rows}
    assert queues == {"default", "other"}
    assert "tests.tasks.add" in names

    # the queue filter sidebar offers both queues
    sidebar = soup.select_one("#changelist-filter")
    assert sidebar is not None
    assert sidebar.select_one('a[href*="queue=default"]') is not None
    assert sidebar.select_one('a[href*="queue=other"]') is not None

    # filtering by queue narrows the actual result rows
    fsoup = parse_html(client.get(url, {"queue": "other"}))
    frows = result_rows(fsoup)
    fqueues = {r.select_one(".field-queue").get_text(strip=True) for r in frows}
    fnames = {r.select_one(".field-task_name").get_text(strip=True) for r in frows}
    assert fqueues == {"other"}
    assert "tests.tasks.boom" not in fnames  # boom is on the default queue only


def test_changelist_shows_mixed_task_states(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed_mixed()
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_task_changelist")
    soup = parse_html(client.get(cl))
    states = {
        r.select_one(".field-state").get_text(strip=True) for r in result_rows(soup)
    }
    assert {"pending", "completed", "failed"} <= states


def test_changelist_filters_by_state(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed_mixed()
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_task_changelist")

    failed = parse_html(client.get(cl, {"state": "failed"}))
    assert {
        r.select_one(".field-state").get_text(strip=True) for r in result_rows(failed)
    } == {"failed"}

    pending = parse_html(client.get(cl, {"state": "pending"}))
    assert {
        r.select_one(".field-state").get_text(strip=True) for r in result_rows(pending)
    } == {"pending"}


def test_changelist_search_narrows_by_task_name(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed_mixed()  # two add tasks + one boom
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_task_changelist")
    soup = parse_html(client.get(cl, {"q": "tests.tasks.boom"}))
    names = {
        r.select_one(".field-task_name").get_text(strip=True) for r in result_rows(soup)
    }
    assert names == {"tests.tasks.boom"}


def test_failed_task_detail_shows_state(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    # the backend's result id is already "<queue>:<task_id>" — i.e. the admin_pk
    url = reverse("admin:django_absurd_task_change", args=[quote(failed.id)])
    soup = parse_html(client.get(url))
    assert soup.select_one(".field-state .readonly").get_text(strip=True) == "failed"


def test_detail_for_missing_object_does_not_500(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed_mixed()
    client.force_login(admin_user)
    bogus = quote(f"default:{uuid.uuid4()}")
    resp = client.get(reverse("admin:django_absurd_task_change", args=[bogus]))
    assert resp.status_code in (302, 404)
    qresp = client.get(
        reverse("admin:django_absurd_queue_change", args=[quote("nonexistent")])
    )
    assert qresp.status_code in (302, 404)


def test_changelist_warns_about_unindexed_queue(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    # enqueue auto-creates 'other' (catalog + physical tables) but does NOT
    # rebuild the views, so 'other' is absent from the union view's arms.
    add.using(queue_name="other").enqueue(7, 8)
    client.force_login(admin_user)
    soup = parse_html(client.get(reverse("admin:django_absurd_task_changelist")))
    warning = soup.select_one("ul.messagelist li.warning")
    assert warning is not None
    text = warning.get_text()
    assert "other" in text
    assert "absurd_sync_queues" in text


def test_changelist_no_warning_when_all_queues_indexed(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed_mixed()  # syncs + workers → every catalog queue is an arm
    client.force_login(admin_user)
    soup = parse_html(client.get(reverse("admin:django_absurd_task_changelist")))
    assert soup.select_one("ul.messagelist li.warning") is None


def test_changelist_survives_staleness_detection_failure(
    client, admin_user, django_db_blocker
):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    client.force_login(admin_user)
    with django_db_blocker.unblock():
        call_command("migrate", "django_absurd", "zero", verbosity=0)
    try:
        resp = client.get(reverse("admin:django_absurd_task_changelist"))
        assert resp.status_code == 200
    finally:
        with django_db_blocker.unblock():
            call_command("migrate", "django_absurd", verbosity=0)


def test_admin_labels_app_as_absurd(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    # App index: the django_absurd module's caption shows the verbose_name.
    index = parse_html(client.get("/admin/"))
    caption = index.select_one("div.app-django_absurd caption a.section")
    assert caption.get_text(strip=True) == "Absurd"
    # Change-view breadcrumb: the app-index link (blank before the fix — the
    # synthesized models' _meta.app_config resolved to None in the private registry).
    url = reverse("admin:django_absurd_task_change", args=[quote(failed.id)])
    change = parse_html(client.get(url))
    app_crumb = change.select_one('.breadcrumbs a[href="/admin/django_absurd/"]')
    assert app_crumb.get_text(strip=True) == "Absurd"


def test_runs_changelist_filtered_to_task(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    _, failed, _ = seed_mixed()
    client.force_login(admin_user)
    # the task→runs link searches by the bare task_id (admin_pk is "<queue>:<task_id>")
    task_id = failed.id.split(":", 1)[1]
    runs_cl = reverse("admin:django_absurd_run_changelist")
    soup = parse_html(client.get(runs_cl, {"q": task_id}))
    rows = result_rows(soup)
    assert rows  # the failed task has at least one run
    task_ids = {r.select_one(".field-task_id").get_text(strip=True) for r in rows}
    states = {r.select_one(".field-state").get_text(strip=True) for r in rows}
    assert task_ids == {task_id}
    assert "failed" in states


def task_change_url(queue, task_name):
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    obj = (
        build_admin_model(spec).objects.filter(queue=queue, task_name=task_name).first()
    )
    return obj, reverse("admin:django_absurd_task_change", args=[quote(obj.admin_pk)])


def test_task_detail_renders_read_only(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed()
    client.force_login(admin_user)
    client.get(reverse("admin:django_absurd_task_changelist"))  # prime the view
    _, url = task_change_url("default", "tests.tasks.add")

    soup = parse_html(client.get(url))
    # task_name renders as a read-only value, not an editable input
    assert soup.select_one(".field-task_name .readonly") is not None
    assert soup.select_one('input[name="task_name"]') is None
    assert soup.select_one('textarea[name="params"]') is None


def test_task_detail_has_runs_link(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed()
    client.force_login(admin_user)
    client.get(reverse("admin:django_absurd_task_changelist"))  # prime the view
    obj, url = task_change_url("default", "tests.tasks.add")

    soup = parse_html(client.get(url))
    anchor = soup.select_one(".field-runs_link a")
    runs_cl = reverse("admin:django_absurd_run_changelist")
    assert anchor is not None
    assert anchor["href"] == f"{runs_cl}?q={obj.task_id}"


def test_checkpoint_detail_with_nasty_name(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    call_command("absurd_sync_queues")
    tid = uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, 'committed')",
            [tid, "step/a:b c", '{"x": 1}'],
        )
    client.force_login(admin_user)
    pk = f"default:{tid}:step/a:b c"
    url = reverse("admin:django_absurd_checkpoint_change", args=[quote(pk)])

    soup = parse_html(client.get(url))
    name_field = soup.select_one(".field-checkpoint_name .readonly")
    assert name_field is not None
    assert name_field.get_text(strip=True) == "step/a:b c"


def test_add_view_forbidden(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    client.force_login(admin_user)
    url = reverse("admin:django_absurd_task_add")
    assert client.get(url).status_code in (403, 302)


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {
                "ENABLE_ADMIN": False,
                "ADMIN_SITE": ("tests.admin.gated_site",),
            },
        }
    }
)
def test_admin_disabled_skips_registration():
    from tests.admin import gated_site  # noqa: PLC0415

    autoregister_admin()
    assert not any(m._meta.app_label == "django_absurd" for m in gated_site._registry)
    # flipping the switch on the same site registers — proves the gate, not emptiness
    enabled = {
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {
                "ENABLE_ADMIN": True,
                "ADMIN_SITE": ("tests.admin.gated_site",),
            },
        }
    }
    with override_settings(TASKS=enabled):
        autoregister_admin()
    assert any(m._meta.model_name == "task" for m in gated_site._registry)


@override_settings(
    TASKS={
        "default": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {
                "ADMIN_SITE": ("tests.admin.custom_site", "tests.admin.other_site")
            },
        }
    }
)
def test_admin_site_tuple_registers_on_all_sites():
    from tests.admin import custom_site, other_site  # noqa: PLC0415

    register_absurd_admin(resolve_admin_sites())
    assert any(m._meta.model_name == "task" for m in custom_site._registry)
    assert any(m._meta.model_name == "task" for m in other_site._registry)


def test_run_detail_shows_failure_reason(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    seed_mixed()
    client.force_login(admin_user)
    runs_cl = reverse("admin:django_absurd_run_changelist")
    client.get(runs_cl)  # prime the runs view
    runs_model = build_admin_model(
        next(s for s in ADMIN_ENTITY_SPECS if s.name == "runs")
    )
    run = runs_model.objects.filter(queue="default", state="failed").first()
    url = reverse("admin:django_absurd_run_change", args=[quote(run.admin_pk)])
    soup = parse_html(client.get(url))
    failure = soup.select_one(".field-failure_reason .readonly")
    assert failure is not None
    text = failure.get_text()
    assert "boom" in text or "ValueError" in text


def test_events_changelist_and_detail(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."e_default" (event_name, payload) VALUES (%s, %s)',
            ["order.shipped", '{"id": 1}'],
        )
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_event_changelist")
    soup = parse_html(client.get(cl))
    names = {
        r.select_one(".field-event_name").get_text(strip=True)
        for r in result_rows(soup)
    }
    assert "order.shipped" in names

    url = reverse(
        "admin:django_absurd_event_change", args=[quote("default:order.shipped")]
    )
    detail = parse_html(client.get(url))
    assert (
        detail.select_one(".field-event_name .readonly").get_text(strip=True)
        == "order.shipped"
    )


def test_waits_changelist_and_composite_detail(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    call_command("absurd_sync_queues")
    rid, tid = uuid.uuid4(), uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."w_default" (task_id, run_id, step_name, event_name)'
            " VALUES (%s, %s, %s, %s)",
            [tid, rid, "wait/step:1", "evt"],
        )
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_wait_changelist")
    soup = parse_html(client.get(cl))
    steps = {
        r.select_one(".field-step_name").get_text(strip=True) for r in result_rows(soup)
    }
    assert "wait/step:1" in steps

    # composite-PK detail (queue:run_id:step_name) with a nasty step_name
    url = reverse(
        "admin:django_absurd_wait_change",
        args=[quote(f"default:{rid}:wait/step:1")],
    )
    detail = parse_html(client.get(url))
    assert (
        detail.select_one(".field-step_name .readonly").get_text(strip=True)
        == "wait/step:1"
    )


def test_checkpoints_changelist(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    call_command("absurd_sync_queues")
    tid = uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, 'committed')",
            [tid, "cp1", '{"n": 1}'],
        )
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_checkpoint_changelist")
    soup = parse_html(client.get(cl))
    names = {
        r.select_one(".field-checkpoint_name").get_text(strip=True)
        for r in result_rows(soup)
    }
    assert "cp1" in names


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
def test_partitioned_queue_appears_in_admin(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    call_command("absurd_sync_queues")
    add.using(queue_name="part").enqueue(1, 1)
    call_command("absurd_worker", queue="part", burst=True)
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_task_changelist")
    soup = parse_html(client.get(cl))
    queues = {
        r.select_one(".field-queue").get_text(strip=True) for r in result_rows(soup)
    }
    assert "part" in queues


def test_admin_tasks_changelist_degrades_when_view_dropped(client, admin_user):
    register_absurd_admin([djadmin.site])
    refresh_url_resolver()
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS absurd.tasks_view")
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_task_changelist")
    resp = client.get(cl)
    assert resp.status_code == 200
    assert result_rows(parse_html(resp)) == []
    call_command("absurd_sync_queues")
