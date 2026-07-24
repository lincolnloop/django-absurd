import pytest
from django.db import connections
from pytest_django.fixtures import SettingsWrapper

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from tests.pg_cron import utils


def live_database() -> str:
    return str(connections["default"].settings_dict["NAME"])


def test_build_jobname_includes_target_database() -> None:
    assert (
        catalog.build_jobname("app_db", Source.SETTINGS, "nightly")
        == "_dj:app_db:s:nightly"
    )


def test_build_jobname_without_name_is_the_prefix() -> None:
    assert catalog.build_jobname("test_x_gw1", Source.SETTINGS) == "_dj:test_x_gw1:s:"


@pytest.fixture
def _opt_in(settings: SettingsWrapper) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)


@pytest.fixture
def _inert(settings: SettingsWrapper) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_opt_in")
def test_schedule_job_binds_to_app_database() -> None:
    live_db = live_database()
    catalog.schedule_job(
        "default",
        name="probe",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    job = utils.fetch_cron_job(f"_dj:{live_db}:{Source.SETTINGS}:probe")
    assert job is not None
    database, active = job
    assert database == live_db
    assert active is True


@pytest.mark.django_db(transaction=True)
def test_schedule_job_is_noop_when_inert(settings: SettingsWrapper) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    live_db = live_database()
    catalog.schedule_job(
        "default",
        name="probe",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    assert utils.fetch_cron_job(f"_dj:{live_db}:{Source.SETTINGS}:probe") is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_opt_in")
def test_unschedule_job_removes_the_bound_job() -> None:
    live_db = live_database()
    jobname = catalog.build_jobname(live_db, Source.SETTINGS, "probe")
    catalog.schedule_job(
        "default",
        name="probe",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    assert utils.fetch_cron_job(jobname) is not None

    catalog.unschedule_job("default", name="probe", source=Source.SETTINGS)
    assert utils.fetch_cron_job(jobname) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_opt_in")
def test_unschedule_jobs_for_database_clears_one_source_lane() -> None:
    live_db = live_database()
    for name in ("a", "b"):
        catalog.schedule_job(
            "default",
            name=name,
            source=Source.SETTINGS,
            cron="5 seconds",
            command="select 1",
            active=True,
        )
    catalog.schedule_job(
        "default",
        name="kept",
        source=Source.ADMIN,
        cron="5 seconds",
        command="select 1",
        active=True,
    )

    catalog.unschedule_jobs_for_database("default", source=Source.SETTINGS)

    assert [
        r[0] for r in utils.fetch_managed_jobs(live_db, source=Source.SETTINGS)
    ] == []
    assert [r[0] for r in utils.fetch_managed_jobs(live_db, source=Source.ADMIN)] == [
        catalog.build_jobname(live_db, Source.ADMIN, "kept")
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_opt_in")
def test_prune_jobs_removes_jobs_absent_from_keep_names() -> None:
    live_db = live_database()
    for name in ("keep", "stale"):
        catalog.schedule_job(
            "default",
            name=name,
            source=Source.SETTINGS,
            cron="5 seconds",
            command="select 1",
            active=True,
        )

    catalog.prune_jobs("default", source=Source.SETTINGS, keep_names=["keep"])

    assert [
        r[0] for r in utils.fetch_managed_jobs(live_db, source=Source.SETTINGS)
    ] == [catalog.build_jobname(live_db, Source.SETTINGS, "keep")]


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_opt_in")
def test_prune_jobs_tolerates_a_job_removed_out_of_band() -> None:
    live_db = live_database()
    catalog.schedule_job(
        "default",
        name="vanishing",
        source=Source.SETTINGS,
        cron="5 seconds",
        command="select 1",
        active=True,
    )
    jobname = catalog.build_jobname(live_db, Source.SETTINGS, "vanishing")
    with connections["default"].cursor() as cur:
        cur.execute("select jobid from cron.job where jobname = %s", [jobname])
        (jobid,) = cur.fetchone()
        cur.execute("select cron.unschedule(%s)", [jobid])

    catalog.prune_jobs("default", source=Source.SETTINGS, keep_names=[])  # no raise

    assert [
        r[0] for r in utils.fetch_managed_jobs(live_db, source=Source.SETTINGS)
    ] == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_inert")
@pytest.mark.parametrize(
    "verb",
    ["prune_jobs", "schedule_job", "unschedule_job", "unschedule_jobs_for_database"],
)
def test_verbs_are_noop_when_inert(verb: str) -> None:
    live_db = live_database()
    calls = {
        "prune_jobs": lambda: catalog.prune_jobs(
            "default", source=Source.SETTINGS, keep_names=[]
        ),
        "schedule_job": lambda: catalog.schedule_job(
            "default",
            name="x",
            source=Source.SETTINGS,
            cron="5 seconds",
            command="select 1",
            active=True,
        ),
        "unschedule_job": lambda: catalog.unschedule_job(
            "default", name="x", source=Source.SETTINGS
        ),
        "unschedule_jobs_for_database": lambda: catalog.unschedule_jobs_for_database(
            "default", source=Source.SETTINGS
        ),
    }
    calls[verb]()  # returns before opening the central connection

    assert [r[0] for r in utils.fetch_managed_jobs(live_db)] == []
