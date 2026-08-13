import datetime as dt

import pytest
from django.db import connections
from django.tasks import TaskResultStatus, task_backends
from django.utils import timezone
from pytest_django.fixtures import Settings

from django_absurd import absurd_params
from django_absurd.connection import register_jsonb_loader
from django_absurd.queues import get_absurd_client
from django_absurd.test import AbsurdTestRuntime
from tests import tasks

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


def test_run_after_is_accepted_now_that_defer_is_supported(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    run_after = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)

    result = tasks.add.using(run_after=run_after).enqueue(1, 2)

    assert result.status is TaskResultStatus.READY


def test_a_run_after_already_past_runs_on_the_first_drain(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # A wrapper already due never suspends, so both rows run in the one drain.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time():
        run_after = dj_absurd.now - dt.timedelta(hours=1)
        result = tasks.add.using(run_after=run_after).enqueue(1, 2)

        assert [(run.task_name, run.state) for run in dj_absurd.drain()] == [
            ("tests.tasks.add:run_after", "completed"),
            ("tests.tasks.add", "completed"),
        ]
        done = backend.get_result(result.id)
        assert done.status is TaskResultStatus.SUCCESSFUL
        assert done.return_value == 3


def test_a_deferred_task_sleeps_until_run_after_then_runs_once(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.add.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(hours=1))
        assert [(run.task_name, run.state) for run in dj_absurd.drain()] == [
            ("tests.tasks.add:run_after", "completed"),
            ("tests.tasks.add", "completed"),
        ]
        assert backend.get_result(result.id).return_value == 3
        # The caller's id names the wrapper, whose attempts are its own, so count the
        # target's through the id the wrapper recorded.
        inner_id = str(dj_absurd.get_result(result.id).result)
        assert dj_absurd.get_result(inner_id).attempts == 1


def test_a_deferred_task_runs_its_own_steps_exactly_once(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # Nothing is injected into the caller's task any more: its step and its sleep are
    # the only checkpoints in its own namespace, and the wrapper's sleep belongs to a
    # different task entirely. A replayed "bump" would mean the two had merged.
    dj_absurd.sync_queues()
    tasks.SYNC_STEP_CALLS["n"] = 0
    with dj_absurd.freeze_time() as frozen_time:
        tasks.ssleep_for_once.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue("k")

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]  # the wrapper

        frozen_time.shift(dt.timedelta(hours=1))
        assert [(run.task_name, run.state) for run in dj_absurd.drain()] == [
            ("tests.tasks.ssleep_for_once:run_after", "completed"),
            ("tests.tasks.ssleep_for_once", "sleeping"),  # its own nap
        ]

        frozen_time.shift(dt.timedelta(days=8))
        assert [(run.state, run.result) for run in dj_absurd.drain()] == [
            ("completed", 1)
        ]

    assert tasks.SYNC_STEP_CALLS["n"] == 1


def test_a_deferred_task_survives_a_project_that_disables_use_tz(
    dj_absurd: AbsurdTestRuntime, settings: Settings
) -> None:
    # Django only rejects a naive run_after when USE_TZ is on, and timezone.now() is
    # itself naive when it is off — so normalize_to_utc is what keeps the instant the
    # wrapper's sleep_until receives aware, or a deferred task dies far from its cause.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    settings.USE_TZ = False
    with dj_absurd.freeze_time() as frozen_time:
        # timezone.now() is naive under USE_TZ=False — the shape a real project's
        # own code hands to run_after.
        run_after = timezone.now() + dt.timedelta(hours=1)
        result = tasks.add.using(run_after=run_after).enqueue(1, 2)

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]
        assert backend.get_result(result.id).status is TaskResultStatus.READY

        frozen_time.shift(dt.timedelta(hours=1))
        assert [(run.task_name, run.state) for run in dj_absurd.drain()] == [
            ("tests.tasks.add:run_after", "completed"),
            ("tests.tasks.add", "completed"),
        ]
        assert backend.get_result(result.id).return_value == 3


def test_a_waiting_deferred_task_reads_as_ready_with_no_start_times(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time():
        result = tasks.add.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        dj_absurd.drain()

        waiting = backend.get_result(result.id)
        assert waiting.status is TaskResultStatus.READY
        assert waiting.started_at is None
        assert waiting.last_attempted_at is None
        assert waiting.args == [1, 2]  # the caller's args, not the wrapper's


def test_a_woken_deferred_task_reports_the_targets_own_result(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.add.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        dj_absurd.drain()

        done = backend.get_result(result.id)
        assert done.status is TaskResultStatus.SUCCESSFUL
        assert done.return_value == 3


def test_a_deferral_that_cannot_launch_reports_failed(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # A wrapper told to launch its target onto a queue the backend never declared can
    # never launch it. Once it is out of attempts the caller sees FAILED, carrying the
    # launch's own error — the deferral failed, and nothing of theirs ran.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        get_absurd_client().spawn(
            "tests.tasks.add:run_after",
            {
                "args": [],
                "kwargs": {
                    "args": [1, 2],
                    "kwargs": {},
                    "queue": "undeclared",
                    "options": {},
                    "due": (dj_absurd.now + dt.timedelta(hours=1)).isoformat(),
                },
            },
            queue="default",
            max_attempts=1,
        )
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        drained = dj_absurd.drain()

        failed = backend.get_result(f"default:{drained[0].task_id}")
        assert failed.status is TaskResultStatus.FAILED
        assert [error.exception_class_path for error in failed.errors] == [
            "InvalidTask"
        ]


def test_a_deferral_retrying_its_launch_still_reads_as_ready(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # Django has no status for "the deferral is retrying", and READY is the truthful
    # answer to whether the caller's task has started — so a wrapper in retry backoff
    # reads READY too, not the RUNNING that "sleeping" maps to for everything else.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        get_absurd_client().spawn(
            "tests.tasks.add:run_after",
            {
                "args": [],
                "kwargs": {
                    "args": [1, 2],
                    "kwargs": {},
                    "queue": "undeclared",
                    "options": {},
                    "due": (dj_absurd.now + dt.timedelta(hours=1)).isoformat(),
                },
            },
            queue="default",
            max_attempts=3,
            retry_strategy={"kind": "fixed", "base_seconds": 7200},
        )
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        drained = dj_absurd.drain()

        result_id = f"default:{drained[0].task_id}"
        assert dj_absurd.get_result(result_id).state == "sleeping"
        assert backend.get_result(result_id).status is TaskResultStatus.READY


def test_a_deferred_tasks_max_duration_measures_its_body_not_its_wait(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        result = (
            absurd_params(cancellation={"max_duration": 60})
            .bind(tasks.add)
            .using(run_after=dj_absurd.now + dt.timedelta(hours=2))
            .enqueue(1, 2)
        )
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=2))
        dj_absurd.drain()

        assert backend.get_result(result.id).return_value == 3


def test_a_deferred_tasks_max_delay_measures_from_its_due_time(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # max_delay is "cancel if not started within N of enqueue". It must count from when
    # the task was really enqueued — at wake — not from the caller's deferred enqueue.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        result = (
            absurd_params(cancellation={"max_delay": 300})
            .bind(tasks.add)
            .using(run_after=dj_absurd.now + dt.timedelta(hours=2))
            .enqueue(1, 2)
        )
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=2))
        dj_absurd.drain()

        assert backend.get_result(result.id).return_value == 3


def test_the_fixtures_own_read_path_reports_the_wrapper_row(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # dj_absurd.get_result is a separate read path from the backend's and deliberately
    # is NOT redirected: the fixture exists to inspect the state that really exists, so
    # a deferred id reports the wrapper row it names — prefixed name, the wrapper's own
    # params, and the inner task's id as its result.
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.add.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        dj_absurd.drain()

        wrapper = dj_absurd.get_result(result.id)
        assert wrapper.task_name == "tests.tasks.add:run_after"
        assert wrapper.args == []
        assert wrapper.kwargs["args"] == [1, 2]
        assert dj_absurd.get_result(str(wrapper.result)).task_name == "tests.tasks.add"


def test_a_deferred_enqueue_spawns_a_wrapper_named_for_its_target(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    register_jsonb_loader(connections["default"].connection)
    with dj_absurd.freeze_time():
        tasks.add.using(run_after=dj_absurd.now + dt.timedelta(hours=1)).enqueue(1, 2)

    claimed = get_absurd_client().claim_tasks(batch_size=1)
    assert claimed[0]["task_name"] == "tests.tasks.add:run_after"


def test_a_deferred_task_keeps_the_callers_per_call_options(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # bind() options are per-invocation, so they must survive the hop to the inner
    # spawn.
    dj_absurd.sync_queues()
    register_jsonb_loader(connections["default"].connection)
    with dj_absurd.freeze_time() as frozen_time:
        absurd_params(max_attempts=9, headers={"trace": "abc"}).bind(tasks.add).using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(hours=1))
        dj_absurd.drain()

    # The inner task ran inside that second drain, so read its ROW — nothing is
    # claimable.
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "select max_attempts, headers from absurd.t_default "
            "where task_name = 'tests.tasks.add'"
        )
        assert cursor.fetchone() == (9, {"trace": "abc"})


def test_a_deferred_task_runs_on_the_callers_queue(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # Both rows must land on the target's queue, or a worker consuming only that queue
    # never runs the wrapper. A drain of one queue claims only that queue's rows, so
    # finding the rows in this drain IS the proof of where they live — RunSnapshot.queue
    # is the argument passed to drain(), so asserting on it cannot fail.
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        tasks.on_reports.using(
            run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue()
        assert [run.task_name for run in dj_absurd.drain("reports")] == [
            "tests.tasks.on_reports:run_after"
        ]
        frozen_time.shift(dt.timedelta(hours=1))
        # Claim order, which here is execution order: the wrapper wakes and enqueues,
        # then the task it enqueued runs in the same drain.
        assert [run.task_name for run in dj_absurd.drain("reports")] == [
            "tests.tasks.on_reports:run_after",
            "tests.tasks.on_reports",
        ]


def test_a_deferred_task_routed_off_its_own_queue_lands_where_it_was_sent(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # tasks.add's own queue is "default"; routing it to "other" is what makes the
    # wrapper re-route the inner enqueue. A target that already declares the queue it
    # is sent to (tasks.on_reports above) leaves that branch untaken, so this is the
    # case that covers `if target_task.queue_name != queue` in enqueue_deferred_target.
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        tasks.add.using(
            queue_name="other", run_after=dj_absurd.now + dt.timedelta(hours=1)
        ).enqueue(1, 2)
        assert [run.task_name for run in dj_absurd.drain("other")] == [
            "tests.tasks.add:run_after"
        ]

        frozen_time.shift(dt.timedelta(hours=1))
        ran = dj_absurd.drain("other")
        assert ("tests.tasks.add", 3) in [(run.task_name, run.result) for run in ran]


def test_a_deferred_name_sleeps_then_enqueues_its_target(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    with dj_absurd.freeze_time() as frozen_time:
        due = (dj_absurd.now + dt.timedelta(hours=1)).isoformat()
        get_absurd_client().spawn(
            "tests.tasks.add:run_after",
            {
                "args": [],
                "kwargs": {
                    "args": [1, 2],
                    "kwargs": {},
                    "queue": "default",
                    "options": {},
                    "due": due,
                },
            },
            queue="default",
        )

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(hours=1))
        ran = [(run.task_name, run.state) for run in dj_absurd.drain()]
        assert ("tests.tasks.add:run_after", "completed") in ran
        assert ("tests.tasks.add", "completed") in ran


def test_deferred_enqueues_sharing_an_idempotency_key_collapse_to_one_wrapper(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # The caller's key rides to the inner enqueue, so the wrapper carries a derived one
    # — without it the herd spawns a wrapper each and dedupes only on waking; with the
    # caller's key verbatim the wrapper would collide with the target it goes on to
    # enqueue.
    dj_absurd.sync_queues()
    backend = task_backends["default"]
    with dj_absurd.freeze_time() as frozen_time:
        due = dj_absurd.now + dt.timedelta(hours=1)
        bound = absurd_params(idempotency_key="nightly").bind(
            tasks.add.using(run_after=due)
        )

        first = bound.enqueue(1, 2)
        second = bound.enqueue(1, 2)

        assert first.id == second.id
        assert [run.task_name for run in dj_absurd.drain()] == [
            "tests.tasks.add:run_after"
        ]

        frozen_time.move_to(due + dt.timedelta(minutes=1))

        assert [run.task_name for run in dj_absurd.drain()] == [
            "tests.tasks.add:run_after",
            "tests.tasks.add",
        ]
        done = backend.get_result(first.id)
        assert done.status is TaskResultStatus.SUCCESSFUL
        assert done.return_value == 3
