import pytest

from tests.pg_cron.validators.utils import ValidateSubject

pytestmark = pytest.mark.django_db(transaction=True)

# Measured against pg_cron 1.6, plus the two deliberate divergences: expressions
# pg_cron accepts but silently truncates, and the @aliases that name no cadence.
REJECTED: list[tuple[str, str]] = [
    ("* * *", "Expected a 5-field cron expression; got 3 fields."),
    ("0 2 * *", "Expected a 5-field cron expression; got 4 fields."),
    ("0 2 * * * *", "Expected a 5-field cron expression; got 6 fields."),
    ("0 seconds", "An interval schedule must be between 1 and 59 seconds."),
    ("60 seconds", "An interval schedule must be between 1 and 59 seconds."),
    ("0 2 * * ?", "pg_cron cannot parse '#', 'L', 'W', '?', 'R' or 'H'."),
    ("0 2 L * *", "pg_cron cannot parse '#', 'L', 'W', '?', 'R' or 'H'."),
    ("@reboot", "@reboot and @restart do not describe a recurring schedule."),
    ("@restart", "@reboot and @restart do not describe a recurring schedule."),
    ("99 2 * * *", "Not a valid cron expression."),
]

ACCEPTED: list[str] = [
    "0 2 * * *",
    "*/5 * * * *",
    "1-5 * * * *",
    "1,3,5 * * * *",
    "0 0 * JAN *",
    "0 0 * * MON",
    "0 0 * * mon",
    "0 0 * * 7",
    "  0   2 * * *  ",
    "1 seconds",
    "30 seconds",
    "59 seconds",
    "30 second",
    "30 SECONDS",
    "@daily",
    "@hourly",
]


@pytest.mark.parametrize(("cron", "message"), REJECTED)
def test_invalid_expression_is_rejected_without_asking_the_database(
    validate: ValidateSubject,
    cron: str,
    message: str,
) -> None:
    # The plain `validate` subject does NOT opt pg_cron in on the test database, so
    # nothing here can reach the central catalog: every rejection is decided in Python.
    result = validate(cron=cron)
    assert result
    assert message in result


@pytest.mark.parametrize("cron", ACCEPTED)
def test_expression_pg_cron_accepts_is_not_rejected(
    validate: ValidateSubject,
    cron: str,
) -> None:
    assert validate(cron=cron) is None


@pytest.mark.parametrize("cron", ["0 2 * * 5#2", "0 2 15W * *"])
def test_token_pg_cron_cannot_parse_is_rejected(
    validate: ValidateSubject,
    cron: str,
) -> None:
    # Vixie's parser implements none of #, L, W, ?, R or H. It stops at the unknown
    # character and swallows the remainder as command text, so "0 2 * * 5#2" is
    # accepted and means every Friday — not the second Friday.
    result = validate(cron=cron)
    assert result
    assert "pg_cron cannot parse '#', 'L', 'W', '?', 'R' or 'H'." in result
