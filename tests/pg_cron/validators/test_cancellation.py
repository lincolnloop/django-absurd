from tests.pg_cron.validators.utils import validate_from_model


def test_cancellation_max_duration_rejects_non_integer(settings):
    result = validate_from_model(settings, cancellation_max_duration="soon")
    assert result
    assert "“soon” value must be an integer." in result


def test_cancellation_max_delay_rejects_non_integer(settings):
    result = validate_from_model(settings, cancellation_max_delay="soon")
    assert result
    assert "“soon” value must be an integer." in result
