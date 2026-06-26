from django.test import override_settings

from django_absurd.backends import AbsurdBackend
from django_absurd.queues import get_absurd_backend

BACKEND = "django_absurd.backends.AbsurdBackend"


def test_returns_single_backend():
    be = get_absurd_backend()
    assert isinstance(be, AbsurdBackend)


@override_settings(
    TASKS={
        "a": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"ENABLE_ADMIN": False},
        },
        "b": {"BACKEND": BACKEND, "QUEUES": ["default"]},
    }
)
def test_first_in_order_wins_when_sharing_db():
    # both on "default" → first declared ("a") wins
    be = get_absurd_backend()
    assert be.options.get("ENABLE_ADMIN") is False


@override_settings(TASKS={"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}})
def test_returns_none_without_absurd_backend():
    assert get_absurd_backend() is None


@override_settings(
    TASKS={
        "a": {
            "BACKEND": BACKEND,
            "QUEUES": ["default"],
            "OPTIONS": {"DATABASE": "sqlite"},
        },
        "b": {"BACKEND": BACKEND, "QUEUES": ["default"]},
    }
)
def test_skips_backend_not_on_resolved_database():
    # two backends on different DBs → resolve picks "default"; the sqlite-backed "a"
    # is skipped before the default-backed "b" is returned
    assert get_absurd_backend().database == "default"
