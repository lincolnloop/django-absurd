import re
import typing as t

import pytest
from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.urls import reverse

import seed
from django_absurd.exceptions import QueueNotProvisionedError
from django_absurd.queues import resolve_absurd_database

# Seeding drains its template rows with a real `absurd_worker` child: a separate
# process on its own connection, which cannot see rows the test has not committed.
pytestmark = pytest.mark.django_db(transaction=True)

# Small enough to seed in a couple of seconds, and well past the handful of templates
# every clone is copied from, so the clone path does the bulk of the work.
SEEDED_ROWS = 200
# The clone copies the templates round-robin in uuidv7 order, so the one that fails is
# every sixth: 33 of the 200 tasks end failed, and a failed task carries a run per
# attempt where a completed one carries a single run.
SEEDED_RUNS = SEEDED_ROWS + 33


@pytest.mark.parametrize(
    ("drift_ddl", "refusal"),
    [
        (
            "alter table absurd.t_bench add column priority integer",
            (
                "The queue tables are not the shape this seeder clones: "
                "absurd.t_bench has unknown priority. Reconcile TASK_CLONE_COLUMNS "
                "and RUN_CLONE_COLUMNS in benchmarks/seed.py against django_absurd's "
                "migration, then seed again."
            ),
        ),
        (
            "alter table absurd.t_bench drop column params cascade",
            (
                "The queue tables are not the shape this seeder clones: "
                "absurd.t_bench has no params. Reconcile TASK_CLONE_COLUMNS "
                "and RUN_CLONE_COLUMNS in benchmarks/seed.py against django_absurd's "
                "migration, then seed again."
            ),
        ),
    ],
)
@pytest.mark.usefixtures("_isolate_queues")
def test_seeding_refuses_a_queue_table_whose_shape_it_does_not_know(
    drift_ddl: str, refusal: str
) -> None:
    """Both directions, by really altering the table; the error names the column.

    ``cascade`` on the drop because ``params`` is in the tasks entity spec, so every
    database carries an admin view selecting it; ``_isolate_queues`` puts both back.
    """
    call_command("absurd_sync_queues")  # _isolate_queues dropped the queue on setup
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(drift_ddl)

    with pytest.raises(seed.QueueTableShapeError) as caught:
        seed.seed_queue_tables(rows=10, queue="bench")

    assert str(caught.value) == refusal


@pytest.mark.usefixtures("_isolate_queues")
def test_seeding_sends_an_unprovisioned_queue_to_the_command_that_provisions_it() -> (
    None
):
    """A queue with no tables at all is somebody who skipped `migrate`.

    Told apart from drift on purpose: every column reads as missing either way, and the
    shape guard's own message would send them off to edit the clone's column lists over
    a database nothing had ever provisioned.
    """
    with pytest.raises(QueueNotProvisionedError) as caught:
        seed.seed_queue_tables(rows=10, queue="bench")

    assert str(caught.value) == (
        "Queue 'bench' is declared but its Absurd table is not provisioned. "
        "Run: manage.py absurd_sync_queues"
    )


def test_seeding_writes_the_rows_it_reports_and_the_admin_can_page_them(
    admin_client: Client, capsys: pytest.CaptureFixture[str]
) -> None:
    """Counted through the admin's own changelists, which is what a person pages.

    Driven through the command line, so the line a person reads after seeding is
    asserted against the same corpus — with only the elapsed time blanked, the one
    figure that belongs to the machine rather than to the corpus.

    The runs changelist is what the template drain exists for, and its count is
    arithmetic rather than the table read back at itself: a failed task carries a run
    per attempt, so the corpus holds one run per task plus one for each failed one.
    `last_attempt_run` is checked against the runs table too — a clone pointing at the
    template's run instead of its own is a dangling link the admin renders as real.
    """
    seed.main(["--rows", str(SEEDED_ROWS)])
    reported = re.sub(r"[\d.]+s$", "Ns", capsys.readouterr().out.strip())

    tasks_page = admin_client.get(reverse("admin:django_absurd_task_changelist"))
    runs_page = admin_client.get(reverse("admin:django_absurd_run_changelist"))
    tasks_changelist = t.cast("dict[str, t.Any]", tasks_page.context_data)["cl"]
    runs_changelist = t.cast("dict[str, t.Any]", runs_page.context_data)["cl"]
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("select distinct state from absurd.r_bench")
        run_states = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "select count(*) from absurd.t_bench t join absurd.r_bench r "
            "on r.run_id = t.last_attempt_run and r.task_id = t.task_id"
        )
        tasks_whose_last_run_is_their_own = cursor.fetchone()[0]
    assert {
        "reported": reported,
        "pages": (tasks_page.status_code, runs_page.status_code),
        "tasks_the_changelist_found": tasks_changelist.result_count,
        "runs_the_changelist_found": runs_changelist.result_count,
        "run_states": run_states,
        "tasks_whose_last_run_is_their_own": tasks_whose_last_run_is_their_own,
    } == {
        "reported": (
            f"seeded absurd.t_bench: {SEEDED_ROWS} tasks, {SEEDED_RUNS} runs in Ns"
        ),
        "pages": (200, 200),
        "tasks_the_changelist_found": SEEDED_ROWS,
        "runs_the_changelist_found": SEEDED_RUNS,
        "run_states": {"completed", "failed"},
        "tasks_whose_last_run_is_their_own": SEEDED_ROWS,
    }
