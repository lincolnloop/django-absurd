from django_absurd.pg_cron.validators import build_jobname, build_jobname_prefix


def test_jobname_format():
    assert build_jobname("default", "nightly") == "absurd:settings:default:nightly"


def test_jobname_custom_source():
    assert (
        build_jobname("default", "nightly", source="manual")
        == "absurd:manual:default:nightly"
    )


def test_jobname_prefix():
    assert build_jobname_prefix("default") == "absurd:settings:default:"


def test_jobname_prefix_custom_source():
    assert build_jobname_prefix("default", source="manual") == "absurd:manual:default:"
