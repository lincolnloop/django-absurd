from django_absurd.pg_cron.validators import build_jobname, build_jobname_prefix


def test_jobname_format() -> None:
    assert build_jobname("default", "nightly") == "_dj:s:default:nightly"


def test_jobname_custom_source() -> None:
    assert (
        build_jobname("default", "nightly", source="manual")
        == "_dj:manual:default:nightly"
    )


def test_jobname_prefix() -> None:
    assert build_jobname_prefix("default") == "_dj:s:default:"


def test_jobname_prefix_custom_source() -> None:
    assert build_jobname_prefix("default", source="manual") == "_dj:manual:default:"
