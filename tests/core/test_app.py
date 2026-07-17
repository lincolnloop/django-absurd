import importlib
import re
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
    module_file: str | None = module.__file__
    if module_file is None:
        msg = "Module file path is None"
        raise RuntimeError(msg)
    src = Path(module_file)
    assert "from django_absurd.models import" not in src.read_text()
    assert QueueReadOnlyError is ReExportedQueueReadOnlyError
