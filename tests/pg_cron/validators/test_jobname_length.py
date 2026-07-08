from tests.pg_cron.validators.utils import validate_from_model

LONG = "a" * 50  # composed job name exceeds 63 bytes


def test_long_jobname_rejected(settings):
    jobname = f"absurd:admin:default:{LONG}"
    size = len(jobname.encode())
    expected = (
        f"job name exceeds 63 bytes (composed name '{jobname}' is {size} bytes;"
        " Postgres silently truncates longer names)."
    )
    result = validate_from_model(settings, name=LONG)
    assert result
    assert expected in result
