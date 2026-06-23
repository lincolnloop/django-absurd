import re

from django.apps import apps

from django_absurd import ABSURD_SCHEMA_VERSION


def test_app_is_registered():
    assert apps.get_app_config("django_absurd").name == "django_absurd"


def test_schema_version_is_concrete_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", ABSURD_SCHEMA_VERSION)
