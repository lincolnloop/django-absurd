import pytest
from django.contrib.auth.models import User
from django.test import Client
from pytest_django import Settings

from tests.pg_cron.validators.utils import (
    ValidateSubject,
    validate_from_admin_post,
    validate_from_model,
    validate_from_system_check,
)


@pytest.fixture(params=["check", "form", "model"])
def validate(
    admin_user: User,
    capsys: pytest.CaptureFixture[str],
    client: Client,
    request: pytest.FixtureRequest,
    settings: Settings,
) -> ValidateSubject:
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
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
    settings: Settings,
) -> ValidateSubject:
    """Subjects for rules the admin form cannot express (e.g. a non-JSON Python
    object for args/kwargs is not a form text input): the system check + full_clean."""
    if request.param == "check":
        return lambda **kwargs: validate_from_system_check(settings, capsys, **kwargs)
    return lambda **kwargs: validate_from_model(settings, **kwargs)
