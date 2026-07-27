from django_absurd.connection import open_central_connection


def test_pg_cron_extension_available() -> None:
    with open_central_connection("default") as cur:
        cur.execute("select extversion from pg_extension where extname = 'pg_cron'")
        row = cur.fetchone()
    assert row is not None, "pg_cron extension not installed on the central DB"
    major, minor = (int(p) for p in row[0].split(".")[:2])
    assert (major, minor) >= (1, 4), f"pg_cron {row[0]} < 1.4"


def test_can_schedule_and_unschedule() -> None:
    with open_central_connection("default") as cur:
        cur.execute(
            "select cron.schedule(%s, %s, %s)",
            ["_dj:__probe__", "* * * * *", "select 1"],
        )
        row = cur.fetchone()
        assert row is not None
        jobid = row[0]
        cur.execute("select count(*) from cron.job where jobid = %s", [jobid])
        assert cur.fetchone() == (1,), "cron.schedule did not create a job row"

        cur.execute("select cron.unschedule(%s)", [jobid])
        cur.execute("select count(*) from cron.job where jobid = %s", [jobid])
        assert cur.fetchone() == (0,), "cron.unschedule did not remove the job row"
