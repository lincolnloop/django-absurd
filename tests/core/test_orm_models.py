from __future__ import annotations

import typing as t

import pytest
from django.apps import apps as global_apps
from django.core.management import call_command
from django.db.models import Count

import django_absurd.models
from django_absurd import models as dm
from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    build_admin_model,
    build_queue_table_model,
)
from django_absurd.exceptions import QueueReadOnlyError
from django_absurd.models import Checkpoint, Event, Run, Task, Wait
from django_absurd.params import AbsurdSpawnParams
from tests.tasks import add, boom


def test_models_importable_and_view_backed() -> None:
    assert Task._meta.db_table == '"absurd"."tasks_view"'
    assert Task._meta.managed is False
    assert Run._meta.db_table == '"absurd"."runs_view"'
    assert Run._meta.managed is False
    assert Checkpoint._meta.db_table == '"absurd"."checkpoints_view"'
    assert Checkpoint._meta.managed is False
    assert Event._meta.db_table == '"absurd"."events_view"'
    assert Event._meta.managed is False
    assert Wait._meta.db_table == '"absurd"."waits_view"'
    assert Wait._meta.managed is False


def test_view_models_absent_from_global_registry() -> None:
    _ = django_absurd.models  # ensure module imported
    names = {
        m.__name__
        for m in global_apps.get_models()
        if m._meta.app_label == "django_absurd"
    }
    assert names == {"Queue"}  # dynamic view-backed models are absent
    pg_cron_names = {
        m.__name__
        for m in global_apps.get_models()
        if m._meta.app_label == "django_absurd_pg_cron"
    }
    assert pg_cron_names == set()


def test_queue_table_model_db_table_is_quote_safe() -> None:
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    assert build_queue_table_model(spec, "default")._meta.db_table == (
        '"absurd"."t_default"'
    )
    # a double-quote in the queue name must be escaped inside the identifier,
    # not break out of it into malformed SQL.
    assert build_queue_table_model(spec, 'a"b')._meta.db_table == ('"absurd"."t_a""b"')


def test_admin_uses_the_models_py_classes() -> None:
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    assert build_admin_model(spec) is dm.Task  # idempotent factory → same class


def seed_two_queues() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(2, 3)
    add.using(queue_name="other").enqueue(7, 8)
    boom.enqueue(  # type: ignore[call-arg]
        absurd_spawn_params=AbsurdSpawnParams(max_attempts=1)
    )
    call_command("absurd_worker", queue="default", burst=True)
    call_command("absurd_worker", queue="other", burst=True)


@pytest.mark.django_db(transaction=True)
def test_filter_across_and_per_queue() -> None:
    seed_two_queues()
    task_model: t.Any = Task
    assert {r.queue for r in task_model.objects.all()} == {"default", "other"}
    assert task_model.objects.filter(queue="other").count() == 1
    assert task_model.objects.filter(state="completed").count() == 2


@pytest.mark.django_db(transaction=True)
def test_cross_queue_aggregate_and_order() -> None:
    seed_two_queues()
    task_model: t.Any = Task
    by_queue = dict(task_model.objects.values_list("queue").annotate(n=Count("*")))
    assert by_queue["other"] == 1
    assert by_queue["default"] >= 2
    recent = list(task_model.objects.order_by("-enqueue_at")[:2])
    assert len(recent) == 2
    assert recent[0].enqueue_at >= recent[1].enqueue_at


@pytest.mark.django_db(transaction=True)
def test_read_only_save_blocked() -> None:
    with pytest.raises(QueueReadOnlyError):
        Task().save()
    with pytest.raises(QueueReadOnlyError):
        Task().delete()


def test_queue_table_model_is_read_only() -> None:
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    model = build_queue_table_model(spec, "default")
    with pytest.raises(QueueReadOnlyError):
        model().save()
    with pytest.raises(QueueReadOnlyError):
        model().delete()
