import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    build_admin_model,
    rebuild_views,
)

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
