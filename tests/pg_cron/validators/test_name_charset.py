import pytest

from tests.pg_cron.validators.utils import ValidateSubject

NAME_MSG = "Schedule name contains characters other than [A-Za-z0-9_-]."
BAD = ["dot.dot", "has space", "unicodé", "with/slash"]
GOOD = ["MixedCase123", "ok", "with-dash", "with_underscore"]


# name IS on the create form, but it's read-only on the change form
# (set once at create, immutable after). Rather than split subjects by
# form, enforce this rule model-first via the check + model subjects —
# both still assert the complete message.
@pytest.mark.parametrize("name", BAD)
def test_bad_name_rejected(
    validate_check_and_model: ValidateSubject,
    name: str,
) -> None:
    result = validate_check_and_model(name=name)
    assert result
    assert NAME_MSG in result


@pytest.mark.parametrize("name", GOOD)
def test_good_name_accepted(
    validate_check_and_model: ValidateSubject,
    name: str,
) -> None:
    result = validate_check_and_model(name=name)
    assert not result or NAME_MSG not in result
