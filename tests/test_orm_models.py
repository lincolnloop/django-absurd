from django.apps import apps as global_apps

import django_absurd.models
from django_absurd import models as dm
from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_admin_model
from django_absurd.models import Checkpoint, Event, Run, Task, Wait


def test_models_importable_and_view_backed():
    assert Task._meta.db_table == 'absurd"."tasks_view'
    assert Task._meta.managed is False
    assert Run._meta.db_table == 'absurd"."runs_view'
    assert Run._meta.managed is False
    assert Checkpoint._meta.db_table == 'absurd"."checkpoints_view'
    assert Checkpoint._meta.managed is False
    assert Event._meta.db_table == 'absurd"."events_view'
    assert Event._meta.managed is False
    assert Wait._meta.db_table == 'absurd"."waits_view'
    assert Wait._meta.managed is False


def test_view_models_absent_from_global_registry():
    _ = django_absurd.models  # ensure module imported
    names = {
        m.__name__
        for m in global_apps.get_models()
        if m._meta.app_label == "django_absurd"
    }
    assert names == {"Queue"}  # only the real managed=False Queue is global


def test_admin_uses_the_models_py_classes():
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    assert build_admin_model(spec) is dm.Task  # idempotent factory → same class
