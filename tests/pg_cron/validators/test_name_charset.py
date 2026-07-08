import pytest

NAME_MSG = "Schedule name contains characters other than [A-Za-z0-9_-]."
BAD = ["dot.dot", "has space", "unicodé", "with/slash"]
GOOD = ["MixedCase123", "ok", "with-dash", "with_underscore"]


@pytest.mark.parametrize("name", BAD)
def test_bad_name_rejected(validate, name):
    result = validate(name=name)
    assert result
    assert NAME_MSG in result


@pytest.mark.parametrize("name", GOOD)
def test_good_name_accepted(validate, name):
    result = validate(name=name)
    assert not result or NAME_MSG not in result
