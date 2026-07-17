import collections.abc

import pytest

pytestmark = pytest.mark.django_db(transaction=True)

# pg_cron 1.6's stable HINT line for an invalid schedule expression; the full
# message is "invalid schedule: <cron>\n" + this HINT (the <cron> is the input).
PG_CRON_INVALID_HINT = (
    "HINT:  Use cron format (e.g. 5 4 * * *), or interval format '[1-59] seconds'"
)

BAD: list[str] = ["* * *", "1 hour", "not a cron"]
GOOD: list[str] = ["*/5 * * * *", "0 2 * * *", "30 seconds"]


@pytest.mark.parametrize("cron", GOOD)
def test_valid_pg_cron_expression_accepted(
    validate_model_and_form: collections.abc.Callable[..., str | None],
    cron: str,
) -> None:
    assert validate_model_and_form(cron=cron) is None


@pytest.mark.parametrize("cron", BAD)
def test_invalid_pg_cron_expression_rejected(
    validate_model_and_form: collections.abc.Callable[..., str | None],
    cron: str,
) -> None:
    result = validate_model_and_form(cron=cron)
    assert result
    expected = f"invalid schedule: {cron}\n{PG_CRON_INVALID_HINT}"
    assert expected in result
