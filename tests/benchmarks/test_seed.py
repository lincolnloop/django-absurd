import re
import typing as t

import pytest
from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.urls import reverse

import seed
from django_absurd.queues import resolve_absurd_database
from tests.benchmarks import utils

# Seeding drains its template rows with a real `absurd_worker` child: a separate
# process on its own connection, which cannot see rows the test has not committed.
pytestmark = pytest.mark.django_db(transaction=True)

# Small enough to seed in a couple of seconds, and well past the handful of templates
# every clone is copied from, so the clone path does the bulk of the work.
SEEDED_ROWS = 200


@pytest.mark.usefixtures("_isolate_queues")
def test_seeding_refuses_a_queue_table_whose_shape_it_does_not_know() -> None:
    """The guard fails the seed, not the read.

    A clone that writes a table it half-understands produces rows that look right and
    are not, so the only safe failure is before any row is written. Driven by really
    altering the table rather than by patching what the seeder believes, and the error
    names the column so the next reader learns which upstream change moved.

    ``cascade`` because ``params`` is in the tasks entity spec, so every database
    carries an admin view selecting it; ``_isolate_queues`` puts both back afterwards.
    """
    call_command("absurd_sync_queues")
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("alter table absurd.t_bench drop column params cascade")

    with pytest.raises(seed.QueueTableShapeError) as caught:
        seed.seed_queue_tables(rows=10, queue="bench")

    assert "params" in str(caught.value)


def test_seeding_writes_the_rows_it_reports_and_the_admin_can_page_them(
    admin_client: Client,
) -> None:
    """Counted through the admin's own changelist, which is what a person pages.

    A seeder returning its intended count reports success for a clone that wrote
    nothing, and a changelist count proves the rows are reachable through the queryset
    the admin actually builds. Runs are asserted separately because enqueueing alone
    produces none, and there are more of them than tasks only if a template really
    failed and was retried before anything was cloned.
    """
    call_command("absurd_sync_queues")

    summary = seed.seed_queue_tables(rows=SEEDED_ROWS, queue="bench")

    response = admin_client.get(reverse("admin:django_absurd_task_changelist"))
    changelist = t.cast("dict[str, t.Any]", response.context_data)["cl"]
    assert {
        "status": response.status_code,
        "rows_the_changelist_found": changelist.result_count,
        "tasks_the_seeder_counted": summary.tasks,
        "runs_the_seeder_counted": summary.runs,
        "more_runs_than_tasks": summary.runs > summary.tasks,
    } == {
        "status": 200,
        "rows_the_changelist_found": SEEDED_ROWS,
        "tasks_the_seeder_counted": SEEDED_ROWS,
        "runs_the_seeder_counted": utils.count_rows("r_bench"),
        "more_runs_than_tasks": True,
    }


def test_the_command_line_seeds_the_queue_it_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The line a person reads after seeding says what the tables now hold.

    Only the elapsed time is blanked: it is the one figure that belongs to the machine
    rather than to the corpus.
    """
    call_command("absurd_sync_queues")
    capsys.readouterr()  # that command reports on stdout too

    seed.main(["--rows", str(SEEDED_ROWS), "--queue", "bench"])

    assert re.sub(r"[\d.]+s$", "Ns", capsys.readouterr().out.strip()) == (
        f"seeded absurd.t_bench: {SEEDED_ROWS} tasks, "
        f"{utils.count_rows('r_bench')} runs in Ns"
    )
