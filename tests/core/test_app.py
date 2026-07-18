import importlib
import re
import typing as t
from pathlib import Path

from django.apps import apps

from django_absurd import ABSURD_SCHEMA_VERSION
from django_absurd.exceptions import QueueReadOnlyError
from django_absurd.models import QueueReadOnlyError as ReExportedQueueReadOnlyError


def test_app_is_registered() -> None:
    assert apps.get_app_config("django_absurd").name == "django_absurd"


def test_schema_version_is_concrete_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", ABSURD_SCHEMA_VERSION)


def test_models_imports_without_cycle() -> None:
    # admin_views must NOT import models (would cycle once models imports the factory)
    module = importlib.import_module("django_absurd.admin_views")
    src = Path(t.cast("str", module.__file__))
    assert "from django_absurd.models import" not in src.read_text()
    assert QueueReadOnlyError is ReExportedQueueReadOnlyError
