import pytest
from django.core.management import call_command
from django.db import connections, models

from django_absurd import admin_views
from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    build_admin_model,
    rebuild_views,
)
from django_absurd.exceptions import ViewNotProvisionedError
from django_absurd.queues import get_absurd_client
from tests.tasks import add

pytestmark = pytest.mark.django_db(transaction=True)


def view_oid(name):
    with connections["default"].cursor() as cur:
        cur.execute("SELECT to_regclass(%s)::oid", [f"absurd.{name}"])
        return cur.fetchone()[0]


def test_rebuild_views_builds_all_five():
    call_command("absurd_sync_queues")
    rebuild_views("default")
    for spec in ADMIN_ENTITY_SPECS:
        assert view_oid(spec.view_name) is not None


def test_read_path_does_no_ddl():
    call_command("absurd_sync_queues")
    rebuild_views("default")
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    before = view_oid(spec.view_name)
    task_model = build_admin_model(spec)
    list(task_model.objects.all())
    list(task_model.objects.filter(state="completed"))
    assert view_oid(spec.view_name) == before


@pytest.mark.django_db(transaction=True)
def test_empty_views_exist_after_migrate_only(django_db_blocker):
    # fresh schema, NO sync, zero queues → views still exist + read empty
    with django_db_blocker.unblock():
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)
        for spec in ADMIN_ENTITY_SPECS:
            assert view_oid(spec.view_name) is not None
        task_cls = build_admin_model(
            next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
        )
        assert list(task_cls.objects.all()) == []


def test_sync_command_rebuilds_views_with_new_queue():
    call_command("absurd_sync_queues")
    task_model = build_admin_model(
        next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    )
    add.using(queue_name="other").enqueue(1, 1)
    call_command("absurd_worker", queue="other", burst=True)
    qs = task_model.objects.values_list("queue", flat=True).distinct()
    assert "other" in set(qs)


def test_self_heal_removed():
    assert not hasattr(admin_views, "ensure_view_current")
    assert not hasattr(admin_views, "VIEW_BUILD_CACHE")


def test_worker_start_rebuilds_when_it_created_queue():
    task_model = build_admin_model(
        next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    )
    call_command("absurd_sync_queues")
    get_absurd_client().drop_queue("other")
    call_command("absurd_sync_queues")
    call_command("absurd_worker", queue="other", burst=True)
    add.using(queue_name="other").enqueue(7, 8)
    call_command("absurd_worker", queue="other", burst=True)
    assert task_model.objects.filter(queue="other").count() >= 1


def test_dropped_queue_read_raises_typed_error():
    call_command("absurd_sync_queues")
    rebuild_views("default")
    with connections["default"].cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS absurd.tasks_view")
    task_model = build_admin_model(
        next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    )
    with pytest.raises(ViewNotProvisionedError):
        list(task_model.objects.all())
    with pytest.raises(ViewNotProvisionedError):
        task_model.objects.count()
    with pytest.raises(ViewNotProvisionedError):
        task_model.objects.exists()
    with pytest.raises(ViewNotProvisionedError):
        task_model.objects.aggregate(models.Count("natural_key"))
    call_command("absurd_sync_queues")
    list(task_model.objects.all())
