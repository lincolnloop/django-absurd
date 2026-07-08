import pytest

from tests.pg_cron.validators.utils import (
    validate_from_model,
    validate_from_system_check,
)


@pytest.fixture(params=["check", "model"])
def validate(request, settings, capsys):
    """Parametrized subject: run a case through each real enforcing entrypoint."""
    if request.param == "check":
        return lambda **kwargs: validate_from_system_check(settings, capsys, **kwargs)
    return lambda **kwargs: validate_from_model(settings, **kwargs)
