import pytest

from tests.pg_cron.validators.utils import validate_from_model


# One rule per field, all real entrypoints: args must be a JSON array, kwargs a JSON
# object. A wrong shape is serializable JSON (so it slips past the serializability
# rule) but breaks task(*args, **kwargs) at fire time.
@pytest.mark.parametrize(
    ("field", "message", "value"),
    [
        ("args", "args must be a JSON array (list).", {"a": 1}),
        ("kwargs", "kwargs must be a JSON object (dict).", [1, 2]),
    ],
)
def test_wrong_shape_rejected(validate, field, message, value):
    result = validate(**{field: value})
    assert result
    assert message in result


# headers is model/admin-only (not a SCHEDULE key), so the check subject can't express
# it — validate it through full_clean. null is allowed; any other non-object is not.
def test_headers_wrong_shape_rejected_by_model(settings):
    result = validate_from_model(settings, headers=[1, 2])
    assert result
    assert "headers must be a JSON object (dict)." in result


def test_headers_object_accepted_by_model(settings):
    assert validate_from_model(settings, headers={"x": "y"}) is None
