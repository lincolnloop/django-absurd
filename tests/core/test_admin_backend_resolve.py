from pytest_django.fixtures import SettingsWrapper

from django_absurd.backends import AbsurdBackend
from django_absurd.queues import get_absurd_backend

BACKEND: str = "django_absurd.backends.AbsurdBackend"


def test_returns_single_backend() -> None:
    be = get_absurd_backend()
    assert isinstance(be, AbsurdBackend)


def test_first_in_order_wins_when_sharing_db(settings: SettingsWrapper) -> None:
    settings.TASKS = {
        "a": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ENABLE_ADMIN": False},
        },
        "b": {"BACKEND": BACKEND, "QUEUES": ["default"]},
    }
    # both on "default" → first declared ("a") wins
    be = get_absurd_backend()
    assert isinstance(be, AbsurdBackend)
    assert be.options.get("ENABLE_ADMIN") is False


def test_returns_none_without_absurd_backend(settings: SettingsWrapper) -> None:
    settings.TASKS = {"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    assert get_absurd_backend() is None


def test_skips_backend_not_on_resolved_database(settings: SettingsWrapper) -> None:
    settings.TASKS = {
        "a": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"DATABASE": "sqlite"},
        },
        "b": {"BACKEND": BACKEND, "QUEUES": ["default"]},
    }
    # two backends on different DBs → resolve picks "default"; the sqlite-backed "a"
    # is skipped before the default-backed "b" is returned
    be = get_absurd_backend()
    assert isinstance(be, AbsurdBackend)
    assert be.database == "default"
