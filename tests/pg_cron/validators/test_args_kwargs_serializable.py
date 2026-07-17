import typing as t

import pytest


# One rule, both subjects: the check validates the raw settings dict; the model's
# JSONField.validate raises the same text (aligned via the field's error_messages).
@pytest.mark.parametrize(
    ("field", "message", "value"),
    [
        ("args", "args is not JSON-serializable.", {1, 2}),
        ("kwargs", "kwargs is not JSON-serializable.", {"a": {1, 2}}),
    ],
)
def test_non_json_rejected(
    validate_check_and_model: t.Callable[..., str | None],
    field: str,
    message: str,
    value: t.Any,
) -> None:
    result = validate_check_and_model(**{field: value})
    assert result
    assert message in result
