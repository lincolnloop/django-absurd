import pytest

from tests.pg_cron.validators.utils import validate_from_model

ALIAS_MSG = "Backend alias contains characters other than [A-Za-z0-9_-]."
# Model subject only: on the check path the alias is the TASKS backend key, driven
# there (not via a SCHEDULE field) — that entrypoint is covered by
# test_pg_cron_checks.test_pg_cron_bad_alias_charset_rejected.
BAD = ["a b", "a.b", "a/b"]
GOOD = ["default", "second-db", "second_db"]


@pytest.mark.parametrize("alias", BAD)
def test_bad_alias_rejected(settings, alias):
    result = validate_from_model(settings, alias=alias)
    assert result
    assert ALIAS_MSG in result


@pytest.mark.parametrize("alias", GOOD)
def test_good_alias_accepted(settings, alias):
    result = validate_from_model(settings, alias=alias)
    assert not result or ALIAS_MSG not in result
