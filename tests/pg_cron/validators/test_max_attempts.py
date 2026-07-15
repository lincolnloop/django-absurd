from tests.pg_cron.validators.utils import validate_from_model


def test_max_attempts_below_one_rejected(settings):
    # Absurd's spawn_task rejects max_attempts < 1; the model's MinValueValidator(1)
    # catches it at authoring instead of at fire time.
    result = validate_from_model(settings, max_attempts=0)
    assert result
    assert "Ensure this value is greater than or equal to 1." in result
