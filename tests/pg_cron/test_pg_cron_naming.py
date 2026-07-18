from django_absurd.pg_cron.validators import build_jobname, build_jobname_prefix


def test_jobname_format() -> None:
    assert build_jobname("nightly") == "_dj:s:nightly"


def test_jobname_custom_source() -> None:
    assert build_jobname("nightly", source="manual") == "_dj:manual:nightly"


def test_jobname_prefix() -> None:
    assert build_jobname_prefix() == "_dj:s:"


def test_jobname_prefix_custom_source() -> None:
    assert build_jobname_prefix(source="manual") == "_dj:manual:"
