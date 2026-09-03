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
from tests.benchmarks import utils

# Seeding drains its template rows with a real `absurd_worker` child: a separate
# process on its own connection, which cannot see rows the test has not committed.
pytestmark = pytest.mark.django_db(transaction=True)

# Small enough to seed in a couple of seconds, and well past the handful of templates
# every clone is copied from, so the clone path does the bulk of the work.
SEEDED_ROWS = 200


@pytest.mark.parametrize(
    ("drift_ddl", "refusal"),
    [
        (
            "alter table absurd.t_bench add column priority integer",
            (
                "The queue tables are not the shape this seeder clones: "
                "absurd.t_bench has unknown priority. Upstream Absurd has moved them, "
                "so the clone would write rows that look right and are not — a column "
                "it has never heard of stays at its default on every cloned row. "
                "Reconcile TASK_CLONE_COLUMNS and RUN_CLONE_COLUMNS in "
                "benchmarks/seed.py against django_absurd's migration, then seed "
                "again."
            ),
        ),
        (
            "alter table absurd.t_bench drop column params cascade",
            (
                "The queue tables are not the shape this seeder clones: "
                "absurd.t_bench has no params. Upstream Absurd has moved them, "
                "so the clone would write rows that look right and are not — a column "
                "it has never heard of stays at its default on every cloned row. "
                "Reconcile TASK_CLONE_COLUMNS and RUN_CLONE_COLUMNS in "
                "benchmarks/seed.py against django_absurd's migration, then seed "
                "again."
            ),
        ),
    ],
)
@pytest.mark.usefixtures("_isolate_queues")
def test_seeding_refuses_a_queue_table_whose_shape_it_does_not_know(
    drift_ddl: str, refusal: str
) -> None:
    """The guard fails the seed, not the read, and it fails in both directions.

    A clone that writes a table it half-understands produces rows that look right and
    are not, so the only safe failure is before any row is written. A column that has
    gone takes the clone's own value with it; a column that has arrived is real on the
    drained templates and left at its default on every row copied from them. Driven by
    really altering the table rather than by patching what the seeder believes, and the
    error names the column so the next reader learns which upstream change moved.

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
    admin_client: Client,
) -> None:
    """Counted through the admin's own changelist, which is what a person pages.

    A seeder returning its intended count reports success for a clone that wrote
    nothing, and a changelist count proves the rows are reachable through the queryset
    the admin actually builds. The runs are asserted twice over: which states reached
    the corpus, which is what the failing template is in the set for, and that there
    are more runs than tasks, which holds only if a template was retried before
    anything was cloned.
    """
    summary = seed.seed_queue_tables(rows=SEEDED_ROWS, queue="bench")

    response = admin_client.get(reverse("admin:django_absurd_task_changelist"))
    changelist = t.cast("dict[str, t.Any]", response.context_data)["cl"]
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("select distinct state from absurd.r_bench")
        run_states = {row[0] for row in cursor.fetchall()}
    assert {
        "status": response.status_code,
        "rows_the_changelist_found": changelist.result_count,
        "tasks_the_seeder_counted": summary.tasks,
        "runs_the_seeder_counted": summary.runs,
        "run_states": run_states,
        "more_runs_than_tasks": summary.runs > summary.tasks,
    } == {
        "status": 200,
        "rows_the_changelist_found": SEEDED_ROWS,
        "tasks_the_seeder_counted": SEEDED_ROWS,
        "runs_the_seeder_counted": utils.count_rows("r_bench"),
        "run_states": {"completed", "failed"},
        "more_runs_than_tasks": True,
    }


def test_the_command_line_reports_what_the_tables_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The line a person reads after seeding says what the tables now hold.

    Only the elapsed time is blanked: it is the one figure that belongs to the machine
    rather than to the corpus.
    """
    seed.main(["--rows", str(SEEDED_ROWS)])

    assert re.sub(r"[\d.]+s$", "Ns", capsys.readouterr().out.strip()) == (
        f"seeded absurd.t_bench: {SEEDED_ROWS} tasks, "
        f"{utils.count_rows('r_bench')} runs in Ns"
    )
