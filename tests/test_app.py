import importlib
import re
from pathlib import Path

from django.apps import apps

import django_absurd.admin_views  # noqa: F401
from django_absurd import ABSURD_SCHEMA_VERSION
from django_absurd.exceptions import QueueReadOnlyError
from django_absurd.models import QueueReadOnlyError as ReExportedQueueReadOnlyError


def test_app_is_registered():
    assert apps.get_app_config("django_absurd").name == "django_absurd"


def test_schema_version_is_concrete_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", ABSURD_SCHEMA_VERSION)


def test_models_imports_without_cycle():
    # admin_views must NOT import models (would cycle once models imports the factory)
    src = Path(importlib.import_module("django_absurd.admin_views").__file__)
    assert "from django_absurd.models import" not in src.read_text()
    assert QueueReadOnlyError is ReExportedQueueReadOnlyError
