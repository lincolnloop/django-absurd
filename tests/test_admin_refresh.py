import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    VIEW_BUILD_CACHE,
    build_admin_model,
    ensure_view_current,
    reset_view_cache,
)
from django_absurd.queues import get_absurd_client
from tests.tasks import add

pytestmark = pytest.mark.django_db(transaction=True)
TASKS_SPEC = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")


def view_exists():
    with connections["default"].cursor() as cur:
        cur.execute("SELECT to_regclass('absurd.tasks_view') IS NOT NULL")
        return cur.fetchone()[0]


def test_first_call_builds_view():
    reset_view_cache()
    call_command("absurd_sync_queues")
    assert view_exists() is False
    ensure_view_current(TASKS_SPEC, "default")
    assert view_exists() is True


def test_new_queue_picked_up_on_next_call():
    reset_view_cache()
    call_command("absurd_sync_queues")
    get_absurd_client().drop_queue("other")  # start with catalog = {default}
    ensure_view_current(TASKS_SPEC, "default")
    add.enqueue(2, 3)
    call_command("absurd_worker", queue="default", burst=True)
    tasks_model = build_admin_model(TASKS_SPEC)
    assert {q for (q,) in tasks_model.objects.values_list("queue").distinct()} == {
        "default"
    }
    # 'other' reappears in the catalog → next ensure rebuilds to include it
    call_command("absurd_sync_queues")
    add.using(queue_name="other").enqueue(7, 8)
    call_command("absurd_worker", queue="other", burst=True)
    ensure_view_current(TASKS_SPEC, "default")
    assert {q for (q,) in tasks_model.objects.values_list("queue").distinct()} == {
        "default",
        "other",
    }


def test_dropped_queue_rebuild_excludes_it():
    reset_view_cache()
    call_command("absurd_sync_queues")
    ensure_view_current(TASKS_SPEC, "default")
    get_absurd_client().drop_queue("other")
    ensure_view_current(TASKS_SPEC, "default")  # catalog changed → rebuild
    assert view_exists() is True
    assert VIEW_BUILD_CACHE[TASKS_SPEC.view_name] == frozenset({"default"})


def fetch_view_oid():
    with connections["default"].cursor() as cur:
        cur.execute(
            "SELECT oid FROM pg_class WHERE relname = %s"
            " AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'absurd')",
            [TASKS_SPEC.view_name],
        )
        return cur.fetchone()[0]


def test_unchanged_catalog_skips_rebuild():
    reset_view_cache()
    call_command("absurd_sync_queues")
    ensure_view_current(TASKS_SPEC, "default")
    oid_before = fetch_view_oid()
    ensure_view_current(TASKS_SPEC, "default")
    oid_after = fetch_view_oid()
    assert oid_before == oid_after
