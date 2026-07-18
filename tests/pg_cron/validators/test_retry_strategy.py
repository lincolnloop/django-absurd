from pytest_django.fixtures import SettingsWrapper

from tests.pg_cron.validators.utils import validate_from_model


def test_retry_kind_invalid_choice_rejected(
    settings: SettingsWrapper,
) -> None:
    result = validate_from_model(settings, retry_kind="bogus")
    assert result
    assert "Value 'bogus' is not a valid choice." in result


def test_retry_timing_without_kind_rejected(
    settings: SettingsWrapper,
) -> None:
    result = validate_from_model(settings, retry_base_seconds=1.5)
    assert result
    assert "Set a retry kind to configure retry timing." in result
