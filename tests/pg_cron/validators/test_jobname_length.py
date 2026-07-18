import pytest_django.fixtures

from tests.pg_cron.validators.utils import validate_from_model

# The recipe is source="a" (admin), so the jobname prefix "_dj:a:" is 6 bytes and the
# name may use the remaining 63 - 6 = 57. The short source code (a, not admin) is what
# buys those bytes: under "admin" the prefix would be 10 bytes, leaving only 53, so a
# 54-to-57 byte name is accepted only because the source is abbreviated.
PREFIX = "_dj:a:"
MAX_NAME = 63 - len(PREFIX)  # 57


def test_name_filling_the_jobname_budget_is_accepted(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    # 57-byte name → composed jobname is exactly 63 bytes → allowed (would have exceeded
    # under the old full-length "admin" source).
    assert validate_from_model(settings, name="a" * MAX_NAME) is None


def test_name_over_the_jobname_budget_is_rejected(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    name = "a" * (MAX_NAME + 1)
    jobname = f"{PREFIX}{name}"
    size = len(jobname.encode())
    expected = (
        f"job name exceeds 63 bytes (composed name '{jobname}' is {size} bytes;"
        " Postgres silently truncates longer names)."
    )
    result = validate_from_model(settings, name=name)
    assert result
    assert expected in result
