import os

import pytest

from django_absurd import models as absurd_models
from django_absurd.test import AbsurdTestRuntime
from loadtest import models, tasks


def test_migrate_provisions_every_declared_queue() -> None:
    assert set(absurd_models.Queue.objects.values_list("queue_name", flat=True)) == {
        "alpha",
        "beta",
        "bulk",
        "gamma",
    }


@pytest.mark.django_db(transaction=True)
def test_the_sync_workload_task_logs_one_execution(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time():
        result = tasks.burn_sync.using(queue_name="bulk").enqueue({"n": 3})
        runs = dj_absurd.drain(queue="bulk")

    assert [(run.state, run.result) for run in runs] == [("completed", 3)]
    log = models.ExecutionLog.objects.get()
    assert (f"bulk:{log.task_id}", log.pid) == (result.id, os.getpid())


@pytest.mark.django_db(transaction=True)
def test_the_async_workload_task_logs_one_execution(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time():
        result = tasks.burn_async.using(queue_name="bulk").enqueue({"n": 3})
        runs = dj_absurd.drain(queue="bulk")

    assert [(run.state, run.result) for run in runs] == [("completed", 3)]
    log = models.ExecutionLog.objects.get()
    assert (f"bulk:{log.task_id}", log.pid) == (result.id, os.getpid())
