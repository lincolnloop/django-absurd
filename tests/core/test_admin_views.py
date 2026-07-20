import typing as t
import uuid

import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    EntitySpec,
    build_admin_model,
    fetch_catalog_queues,
    rebuild_admin_view,
)
from django_absurd.queues import get_absurd_client
from tests.tasks import add

pytestmark = pytest.mark.django_db(transaction=True)


TASKS_SPEC: EntitySpec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
CHECKS_SPEC: EntitySpec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "checkpoints")


def seed_two_queues() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(2, 3)
    add.using(queue_name="other").enqueue(7, 8)
    call_command("absurd_worker", queue="default", burst=True)
    call_command("absurd_worker", queue="other", burst=True)


def test_zero_queue_view_is_empty() -> None:
    call_command("absurd_sync_queues")  # tables exist but no tasks
    rebuild_admin_view(TASKS_SPEC, [], "default")
    tasks_model: t.Any = build_admin_model(TASKS_SPEC)
    assert tasks_model.objects.count() == 0


def test_union_spans_queues_and_filters() -> None:
    seed_two_queues()
    rebuild_admin_view(
        TASKS_SPEC,
        fetch_catalog_queues("default"),
        "default",
    )
    tasks_model: t.Any = build_admin_model(TASKS_SPEC)
    assert {row.queue for row in tasks_model.objects.all()} == {"default", "other"}
    assert tasks_model.objects.filter(queue="other").count() == 1


def test_jsonb_decodes_and_pk_prefixed() -> None:
    seed_two_queues()
    rebuild_admin_view(
        TASKS_SPEC,
        fetch_catalog_queues("default"),
        "default",
    )
    tasks_model: t.Any = build_admin_model(TASKS_SPEC)
    row = tasks_model.objects.filter(
        queue="default",
        task_name="tests.tasks.add",
    ).first()
    assert isinstance(row.params, dict)
    assert row.natural_key.startswith("default:")


def test_composite_pk_detail_lookup() -> None:
    seed_two_queues()
    tid = uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default"'
            " (task_id, checkpoint_name, state, status)"
            " VALUES (%s, %s, %s, 'committed')",
            [tid, "step/a:b c", '{"x": 1}'],
        )
    rebuild_admin_view(
        CHECKS_SPEC,
        fetch_catalog_queues("default"),
        "default",
    )
    checks_model: t.Any = build_admin_model(CHECKS_SPEC)
    pk = f"default:{tid}:step/a:b c"
    assert checks_model.objects.get(pk=pk).status == "committed"


def test_rebuild_after_drop_excludes_queue() -> None:
    seed_two_queues()
    rebuild_admin_view(TASKS_SPEC, ["default", "other"], "default")
    get_absurd_client().drop_queue("other")
    rebuild_admin_view(
        TASKS_SPEC,
        fetch_catalog_queues("default"),
        "default",
    )
    tasks_model: t.Any = build_admin_model(TASKS_SPEC)
    assert {row.queue for row in tasks_model.objects.all()} == {"default"}
