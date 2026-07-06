from django_absurd.pg_cron.reconcile import build_jobname, jobname_prefix


def test_jobname_format():
    assert build_jobname("default", "nightly") == "absurd:settings:default:nightly"


def test_jobname_custom_source():
    assert (
        build_jobname("default", "nightly", source="manual")
        == "absurd:manual:default:nightly"
    )


def test_jobname_prefix():
    assert jobname_prefix("default") == "absurd:settings:default:"


def test_jobname_prefix_custom_source():
    assert jobname_prefix("default", source="manual") == "absurd:manual:default:"
