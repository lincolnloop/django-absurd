import typing as t

from django.tasks import task_backends
from pytest_django.fixtures import SettingsWrapper

from django_absurd.backends import (
    AbsurdBackend,
    get_absurd_backends,
    get_declared_queues,
)
from django_absurd.queues import resolve_absurd_database

ABSURD = "django_absurd.backends.AbsurdBackend"
EXTENDED = "tests.backends.ExtendedAbsurdBackend"


def test_default_alias_is_absurd_backend() -> None:
    assert isinstance(task_backends["default"], AbsurdBackend)


def test_form_a_names_only(settings: SettingsWrapper) -> None:
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["emails", "retained"]}}
    backend = t.cast("AbsurdBackend", task_backends["default"])
    assert t.cast("set[str]", backend.queues) == {"emails", "retained"}
    assert backend.database == "default"
    assert backend.default_max_attempts == 5


def test_form_b_pushes_keys_up_and_reads_options(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "DATABASE": "absurd",
                "DEFAULT_MAX_ATTEMPTS": 9,
                "QUEUES": {"emails": {}, "retained": {"storage_mode": "partitioned"}},
            },
        }
    }
    backend = t.cast("AbsurdBackend", task_backends["default"])
    assert t.cast("set[str]", backend.queues) == {"emails", "retained"}
    assert backend.database == "absurd"
    assert backend.default_max_attempts == 9


def test_get_absurd_backends_finds_default() -> None:
    backends = get_absurd_backends()
    assert set(backends) == {"default"}
    assert isinstance(backends["default"], AbsurdBackend)


def test_get_absurd_backends_matches_subclasses(
    settings: SettingsWrapper,
) -> None:
    # ExtendedAbsurdBackend lives in an importable module so TASKS can name it by
    # path; the resolver must find it via isinstance, not class identity.
    settings.TASKS = {"default": {"BACKEND": EXTENDED, "QUEUES": ["x"]}}
    backends = get_absurd_backends()
    assert set(backends) == {"default"}
    assert isinstance(backends["default"], AbsurdBackend)
    assert type(task_backends["default"]).__name__ == "ExtendedAbsurdBackend"


def test_get_declared_queues_form_a_defaults_policy(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["a", "b"]}}
    backend = get_absurd_backends()["default"]
    assert get_declared_queues(backend) == {"a": {}, "b": {}}


def test_get_declared_queues_form_b_preserves_policy(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"a": {}, "b": {"cleanup_limit": 50}}},
        }
    }
    backend = get_absurd_backends()["default"]
    assert get_declared_queues(backend) == {"a": {}, "b": {"cleanup_limit": 50}}


def test_resolve_absurd_database_single(settings: SettingsWrapper) -> None:
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "default"}}
    }
    assert resolve_absurd_database() == "default"


def test_resolve_absurd_database_ambiguous_degrades_to_default(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "default"}},
        "other": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "absurd"}},
    }
    assert resolve_absurd_database() == "default"
