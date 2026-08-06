import typing as t

import pytest
from django.core.management import CommandError, call_command
from django.db import connection

from django_absurd import models as absurd_models

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


def test_seed_creates_the_requested_number_of_task_rows() -> None:
    task_model: t.Any = absurd_models.Task
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)

    assert task_model.objects.filter(queue="bulk").count() == 200


def test_every_seeded_task_gets_a_distinct_id() -> None:
    task_model: t.Any = absurd_models.Task
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)

    rows = task_model.objects.filter(queue="bulk")
    assert rows.values("task_id").distinct().count() == rows.count()


def test_seeded_tasks_spread_across_the_requested_window() -> None:
    task_model: t.Any = absurd_models.Task
    call_command("load_seed", queue=["bulk"], tasks=500, window=30, truncate=True)

    times = list(
        task_model.objects.filter(queue="bulk").values_list("enqueue_at", flat=True)
    )
    assert (max(times) - min(times)).days >= 20


def test_every_seeded_run_points_at_a_seeded_task() -> None:
    run_model: t.Any = absurd_models.Run
    task_model: t.Any = absurd_models.Task
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)

    task_ids = set(
        task_model.objects.filter(queue="bulk").values_list("task_id", flat=True)
    )
    run_task_ids = set(
        run_model.objects.filter(queue="bulk").values_list("task_id", flat=True)
    )
    assert run_task_ids <= task_ids


def test_seed_refuses_to_clone_a_table_with_an_unknown_column() -> None:
    with connection.cursor() as cur:
        cur.execute("alter table absurd.t_bulk add column surprise_col text")

    with pytest.raises(CommandError) as exc:
        call_command("load_seed", queue=["bulk"], tasks=10, truncate=True)
    assert str(exc.value) == (
        "absurd.t_bulk is not the table loadtest knows how to clone: unknown "
        "column(s) surprise_col. Update the clone column list and override map in "
        "loadtest/schema.py to match the current Absurd schema."
    )


def test_seed_refuses_to_clone_a_table_with_a_missing_column() -> None:
    # CASCADE takes the admin union view with it — every column is projected there.
    # The _isolate_queues teardown rebuilds both.
    with connection.cursor() as cur:
        cur.execute("alter table absurd.r_bulk drop column wake_event cascade")

    with pytest.raises(CommandError) as exc:
        call_command("load_seed", queue=["bulk"], tasks=10, truncate=True)
    assert str(exc.value) == (
        "absurd.r_bulk is not the table loadtest knows how to clone: missing "
        "column(s) wake_event. Update the clone column list and override map in "
        "loadtest/schema.py to match the current Absurd schema."
    )


def test_truncate_clears_previously_seeded_rows() -> None:
    task_model: t.Any = absurd_models.Task
    call_command("load_seed", queue=["bulk"], tasks=50, truncate=True)
    call_command("load_seed", queue=["bulk"], tasks=10, truncate=True)

    assert task_model.objects.filter(queue="bulk").count() == 10
