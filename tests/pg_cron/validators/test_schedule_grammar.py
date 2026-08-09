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
    # pg_cron's alias match is case-SENSITIVE (measured), so these are not aliases.
    ("@DAILY", "Not a valid cron expression."),
    ("@Daily", "Not a valid cron expression."),
    ("@bogus", "Not a valid cron expression."),
    # strtoul takes a sign, so pg_cron parses -30 and then fails its 0 < n < 60 range.
    ("-30 seconds", "An interval schedule must be between 1 and 59 seconds."),
    # `%u` is ASCII-only and the suffix match is byte-wise: neither is the interval
    # form to pg_cron, so both fall through to the cron grammar and fail on shape.
    ("٣٠ seconds", "Expected a 5-field cron expression; got 2 fields."),
    ("30 \u017feconds", "Expected a 5-field cron expression; got 2 fields."),
    ("hourly", "Expected a 5-field cron expression; got 1 field."),
    ("0 2 15W * *", "pg_cron cannot parse '#', 'L', 'W', '?', 'R' or 'H'."),
    # Vixie's parser implements none of # L W ? R H: it stops at the unknown character
    # and swallows the rest as command text, so pg_cron reads this as every Friday.
    ("0 2 * * 5#2", "pg_cron cannot parse '#', 'L', 'W', '?', 'R' or 'H'."),
    # Same swallowing after an alias: pg_cron runs "@daily junk" daily.
    ("@daily junk", "Not a valid cron expression."),
    # sscanf's %u wraps at 2**32, so pg_cron reads this as 30 and runs every 30s.
    ("4294967326 seconds", "An interval schedule must be between 1 and 59 seconds."),
    # Same swallowing again: pg_cron takes the trailing comment as the command.
    ("0 2 * * *  # note", "pg_cron cannot parse '#', 'L', 'W', '?', 'R' or 'H'."),
    ("0 2 * * JANUARY", "Not a valid cron expression."),
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
    "0\t2 * * *",
    "0 2 * * MON-FRI",
    "0 2 * JAN-FEB *",
    "0 2 * * 5-1",
    "1 seconds",
    "30 seconds",
    "59 seconds",
    "30 second",
    "30 SECONDS",
    # scanf's literal space matches ZERO or more whitespace, and %u goes through
    # strtoul — all three run every 30s on pg_cron (measured).
    "30seconds",
    "30  seconds",
    "+30 seconds",
    "030 seconds",
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


# Expressions pg_cron REFUSES that we let through. The design permits this direction —
# the error resurfaces at sync, where settings-declared schedules have always reported
# it — but the set is recorded so tightening the matcher is a visible decision rather
# than an accident, and so a new one cannot appear unnoticed.
TOLERATED: list[str] = ["0\n2 * * *", "*/61 * * * *"]


@pytest.mark.parametrize("cron", TOLERATED)
def test_expression_pg_cron_refuses_is_tolerated_until_sync(
    validate: ValidateSubject,
    cron: str,
) -> None:
    assert validate(cron=cron) is None


@pytest.mark.parametrize("cron", ACCEPTED)
def test_expression_pg_cron_accepts_is_not_rejected(
    validate: ValidateSubject,
    cron: str,
) -> None:
    assert validate(cron=cron) is None
