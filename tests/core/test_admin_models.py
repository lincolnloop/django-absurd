import typing as t
import warnings

import pytest
from django.apps import apps as global_apps
from django.db import models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState

from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_admin_model
from django_absurd.models import QueueReadOnlyError

if t.TYPE_CHECKING:
    from django.db.models.fields import Field


def build_all_models() -> list[type[models.Model]]:
    """Build admin models for all entity specs."""
    return [build_admin_model(s) for s in ADMIN_ENTITY_SPECS]


def test_specs_cover_five_entities() -> None:
    names = {s.name for s in ADMIN_ENTITY_SPECS}
    assert names == {"tasks", "runs", "checkpoints", "events", "waits"}


def test_model_maps_schema_quoted_view_unmanaged() -> None:
    tasks_model = build_admin_model(
        next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    )
    assert tasks_model._meta.db_table == '"absurd"."tasks_view"'
    assert tasks_model._meta.managed is False
    assert tasks_model._meta.pk.name == "natural_key"
    assert isinstance(tasks_model._meta.get_field("params"), models.JSONField)


def test_models_absent_from_global_registry() -> None:
    build_all_models()
    names = {
        m.__name__
        for m in global_apps.get_models()
        if m._meta.app_label == "django_absurd"
    }
    assert "Task" not in names


def test_model_str_is_the_natural_key() -> None:
    for spec in ADMIN_ENTITY_SPECS:
        model = build_admin_model(spec)
        assert str(model(natural_key="default:abc")) == "default:abc"


def test_run_has_task_fk_for_inlining_and_task_id_is_unique() -> None:
    tasks = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    runs = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "runs"))
    fk = t.cast("Field[t.Any, t.Any]", runs._meta.get_field("task"))
    assert fk.related_model is tasks
    # FK joins on task_id
    assert t.cast("t.Any", fk).target_field.name == "task_id"
    # attname stays task_id, not task_id_id
    assert t.cast("t.Any", fk).get_attname() == "task_id"
    # view-backed: no real FK constraint
    assert t.cast("t.Any", fk).db_constraint is False
    # required as the FK target
    task_id_field = t.cast("Field[t.Any, t.Any]", tasks._meta.get_field("task_id"))
    assert task_id_field.unique


def test_build_admin_model_is_idempotent() -> None:
    tasks_spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    assert build_admin_model(tasks_spec) is build_admin_model(tasks_spec)


def test_build_models_twice_emits_no_runtime_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        build_all_models()
        build_all_models()


def test_makemigrations_stays_clean() -> None:
    build_all_models()
    loader = MigrationLoader(None, ignore_no_migrations=True)
    ad = MigrationAutodetector(
        loader.project_state(), ProjectState.from_apps(global_apps)
    )
    changes = ad.changes(graph=loader.graph)
    assert changes.get("django_absurd", []) == []


def test_save_is_blocked() -> None:
    tasks_model = build_admin_model(
        next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    )
    with pytest.raises(QueueReadOnlyError):
        tasks_model().save()
