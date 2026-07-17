import collections.abc

import pytest
import pytest_django.fixtures
from django.contrib.auth.models import AbstractBaseUser
from django.test import Client

from tests.pg_cron.validators.utils import (
    validate_from_admin_post,
    validate_from_model,
    validate_from_system_check,
)


@pytest.fixture(params=["check", "form", "model"])
def validate(
    request: pytest.FixtureRequest,
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
    client: Client,
    admin_user: AbstractBaseUser,
) -> collections.abc.Callable[..., str | None]:
    """Parametrized subject: run a case through each real enforcing entrypoint —
    the system check, the admin change-form POST, and ScheduledTask.full_clean()."""
    if request.param == "check":
        return lambda **kwargs: validate_from_system_check(settings, capsys, **kwargs)
    if request.param == "form":
        return lambda **kwargs: validate_from_admin_post(
            client, admin_user, settings, **kwargs
        )
    return lambda **kwargs: validate_from_model(settings, **kwargs)


@pytest.fixture(params=["check", "model"])
def validate_check_and_model(
    request: pytest.FixtureRequest,
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> collections.abc.Callable[..., str | None]:
    """Subjects for rules the admin form cannot express (e.g. a non-JSON Python
    object for args/kwargs is not a form text input): the system check + full_clean."""
    if request.param == "check":
        return lambda **kwargs: validate_from_system_check(settings, capsys, **kwargs)
    return lambda **kwargs: validate_from_model(settings, **kwargs)


@pytest.fixture(params=["form", "model"])
def validate_model_and_form(
    request: pytest.FixtureRequest,
    settings: pytest_django.fixtures.SettingsWrapper,
    client: Client,
    admin_user: AbstractBaseUser,
) -> collections.abc.Callable[..., str | None]:
    """Subjects for rules the system check does not enforce (e.g. cron grammar is
    DB-authoritative, deferred from check time): the admin form POST + full_clean."""
    if request.param == "form":
        return lambda **kwargs: validate_from_admin_post(
            client, admin_user, settings, **kwargs
        )
    return lambda **kwargs: validate_from_model(settings, **kwargs)
