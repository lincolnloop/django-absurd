import pytest
from django.core.exceptions import ImproperlyConfigured
from pytest_django.fixtures import SettingsWrapper

from django_absurd import emit_event

pytestmark = pytest.mark.django_db(transaction=True)


def test_top_level_emit_event_unknown_queue_raises() -> None:
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"Queue 'ghost' is not declared in TASKS QUEUES\. Add it to the QUEUES "
            r"list in your TASKS backend settings\."
        ),
    ):
        emit_event("whatever", queue="ghost")


def test_top_level_emit_event_no_backend_configured_raises(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    with pytest.raises(
        ImproperlyConfigured, match=r"django-absurd: no Absurd backend configured\."
    ):
        emit_event("whatever")


def test_top_level_emit_event_unsynced_queue_raises(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}, "unsynced": {}}},
        }
    }
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"Queue 'unsynced' is declared but its Absurd table is not provisioned\. "
            r"Run: manage\.py absurd_sync_queues"
        ),
    ):
        emit_event("whatever", queue="unsynced")
