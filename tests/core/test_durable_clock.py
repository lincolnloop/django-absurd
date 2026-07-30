import contextlib
import datetime as dt
import functools
import importlib.abc
import importlib.machinery
import sys
import typing as t
import zoneinfo

import psycopg
import pytest
import time_machine
from django.core.exceptions import ImproperlyConfigured
from django.db import connections

from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)

NAIVE_INSTANT_MESSAGE = (
    r"django-absurd: freeze_time\(\) and move_to\(\) need a timezone-aware datetime; a "
    r"naive one is ambiguous and would desynchronise Postgres from Python\. Pass "
    r"tzinfo=datetime\.UTC \(or any zone\)\."
)


def test_a_week_long_sleep_resumes_after_shifting_a_week(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.sleep_a_week.enqueue()
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=7))

        assert [run.state for run in dj_absurd.drain()] == ["completed"]
        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.result == "woke"


def test_shifting_short_of_the_wake_leaves_the_task_sleeping(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time(FROZEN) as frozen_time:
        result = tasks.sleep_a_week.enqueue()
        dj_absurd.drain()

        frozen_time.shift(dt.timedelta(days=6))

        # Without this, a no-op shift() would pass the rest of the test.
        assert dj_absurd.now == FROZEN + dt.timedelta(days=6)
        assert dj_absurd.drain() == []
        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.state == "sleeping"


def test_a_chain_of_two_sleeps_needs_two_shifts(dj_absurd: AbsurdTestRuntime) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.sleep_twice.enqueue()
        dj_absurd.drain()

        frozen_time.shift(dt.timedelta(days=7))
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=3))
        assert [run.state for run in dj_absurd.drain()] == ["completed"]
        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.result == "woke-twice"


def test_an_await_event_timeout_fires_after_shifting_past_it(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.sawait_event_timeout.enqueue(
            "order.packed:never-arrives", timeout=tasks.WEEK_SECONDS
        )
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.result == "timed-out"


def test_a_retry_backoff_runs_the_next_attempt_after_shifting(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.fail_with_long_backoff.enqueue()
        assert [run.attempt for run in dj_absurd.drain()] == [1]
        mid_backoff = dj_absurd.get_result(result.id)
        assert mid_backoff is not None
        assert mid_backoff.state == "sleeping"
        assert mid_backoff.failure is None

        frozen_time.shift(dt.timedelta(hours=1, seconds=1))

        assert [run.attempt for run in dj_absurd.drain()] == [2]


def test_a_cancelled_task_produces_an_empty_drain(dj_absurd: AbsurdTestRuntime) -> None:
    """Cancellation happens inside claim_task, before anything is claimed."""
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.cancellable_after_a_minute.enqueue()

        frozen_time.shift(dt.timedelta(minutes=2))

        assert dj_absurd.drain() == []
        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.state == "cancelled"


def test_an_expired_claim_is_swept_after_shifting_past_the_lease(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        result = tasks.add.enqueue(2, 3)
        utils.claim_one_run("default", claim_timeout=3600)

        frozen_time.shift(dt.timedelta(hours=2))
        dj_absurd.drain()

        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.state == "completed"
        assert snapshot.attempts == 2


def test_freeze_time_stamps_the_frozen_instant_on_enqueue(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    with dj_absurd.freeze_time(FROZEN):
        result = tasks.add.enqueue(2, 3)

        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.enqueued_at == FROZEN


def test_now_is_real_outside_the_block_and_the_frozen_instant_inside(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    before = dj_absurd.now
    assert before.utcoffset() == dt.timedelta(0)
    assert abs(before - dt.datetime.now(dt.UTC)) < dt.timedelta(seconds=30)

    with dj_absurd.freeze_time(FROZEN) as frozen_time:
        assert dj_absurd.now == FROZEN

        frozen_time.shift(dt.timedelta(days=2))
        assert dj_absurd.now == FROZEN + dt.timedelta(days=2)

        frozen_time.move_to(dt.datetime(2026, 3, 1, tzinfo=dt.UTC))
        assert dj_absurd.now == dt.datetime(2026, 3, 1, tzinfo=dt.UTC)

    assert abs(dj_absurd.now - dt.datetime.now(dt.UTC)) < dt.timedelta(seconds=30)


def test_move_to_can_go_backward(dj_absurd: AbsurdTestRuntime) -> None:
    """A backward move_to() reorders which clock moves first (Postgres, then Python)
    so both still land in lockstep, exactly as a forward one does."""
    with dj_absurd.freeze_time(FROZEN) as frozen_time:
        frozen_time.shift(dt.timedelta(days=2))
        assert dj_absurd.now == FROZEN + dt.timedelta(days=2)

        frozen_time.move_to(FROZEN)
        assert dj_absurd.now == FROZEN


def test_leaving_the_block_releases_both_gucs_and_the_python_clock(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """Both halves, so a test can open several windows in sequence.

    The database-level GUC is what a NEW worker session inherits and what outlives the
    process; the session-level one is what Django's own already-open connection stamps
    an ``enqueue()`` with; time-machine is Python's half.
    """
    real_before = dt.datetime.now(dt.UTC)

    with dj_absurd.freeze_time(FROZEN):
        assert utils.read_database_fake_now() == FROZEN.isoformat()
        assert utils.read_session_fake_now() == FROZEN.isoformat()

    assert utils.read_database_fake_now() is None
    # A custom GUC that a session has SET reads back as empty once RESET, not as NULL.
    assert utils.read_session_fake_now() == ""
    assert abs(dt.datetime.now(dt.UTC) - real_before) < dt.timedelta(seconds=30)

    later = dt.datetime(2027, 6, 1, tzinfo=dt.UTC)
    with dj_absurd.freeze_time(later):
        assert dj_absurd.now == later


@pytest.mark.parametrize("mover", ["move_to", "shift"])
def test_a_mover_used_after_the_block_exits_raises(
    dj_absurd: AbsurdTestRuntime, mover: str
) -> None:
    """An escaped handle would otherwise re-freeze durable time silently, from real now,
    and the fixture's crash net would tidy it away unnoticed."""
    with dj_absurd.freeze_time(FROZEN) as frozen_time:
        assert dj_absurd.now == FROZEN

    move_after_exit: t.Callable[[], None] = (
        functools.partial(frozen_time.move_to, FROZEN)
        if mover == "move_to"
        else functools.partial(frozen_time.shift, dt.timedelta(days=1))
    )

    with pytest.raises(
        RuntimeError,
        match=(
            r"django-absurd: this freeze_time\(\) block has already exited, so durable "
            r"time is real again\. Open a new freeze_time\(\) window to move durable "
            r"time again\."
        ),
    ):
        move_after_exit()

    assert utils.read_database_fake_now() is None  # nothing was re-frozen
    assert abs(dj_absurd.now - dt.datetime.now(dt.UTC)) < dt.timedelta(seconds=30)


def test_freeze_time_refuses_to_nest(dj_absurd: AbsurdTestRuntime) -> None:
    """Two frozen instants cannot both be "now" — and an inner exit would restore real
    time under the outer block rather than the instant it froze."""
    with dj_absurd.freeze_time(FROZEN):
        with (
            contextlib.ExitStack() as stack,
            pytest.raises(
                RuntimeError,
                match=(
                    r"django-absurd: freeze_time\(\) is already active, and two frozen "
                    r"instants cannot both be 'now'\. Move the open freeze with "
                    r"move_to\(\)/shift\(\), or leave its with-block before opening "
                    r"another\."
                ),
            ),
        ):
            stack.enter_context(dj_absurd.freeze_time(FROZEN + dt.timedelta(days=1)))

        # The outer window survives a refused nest rather than being torn down by it.
        assert dj_absurd.now == FROZEN


def test_freeze_time_honors_a_non_utc_aware_zone(dj_absurd: AbsurdTestRuntime) -> None:
    chicago = dt.datetime(
        2026, 3, 8, 1, 30, tzinfo=zoneinfo.ZoneInfo("America/Chicago")
    )
    with dj_absurd.freeze_time(chicago) as frozen_time:
        result = tasks.sleep_a_week.enqueue()
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=7))
        dj_absurd.drain()

        # The week crosses the US spring-forward gap, so the expectation is INSTANT
        # arithmetic: shift() adds absolute elapsed time. `chicago + timedelta(days=7)`
        # would be wall-clock arithmetic and lands an hour earlier.
        assert dj_absurd.now == chicago.astimezone(dt.UTC) + dt.timedelta(days=7)
        assert dj_absurd.now.utcoffset() == dt.timedelta(0)
        snapshot = dj_absurd.get_result(result.id)
        assert snapshot is not None
        assert snapshot.enqueued_at == chicago
        assert snapshot.state == "completed"


def test_the_clock_is_correct_on_a_server_whose_timezone_is_not_utc(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """Regression: Django's adapter relabels timestamptz instead of converting it."""
    dbname = connections["default"].settings_dict["NAME"]
    params: dict[str, t.Any] = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    params.pop("context", None)
    conn = psycopg.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL(
                    "alter database {} set timezone = 'America/Chicago'"
                ).format(psycopg.sql.Identifier(dbname))
            )
        with dj_absurd.freeze_time(FROZEN):
            result = tasks.add.enqueue(2, 3)
            dj_absurd.drain()

            assert dj_absurd.now == FROZEN
            assert dj_absurd.now.utcoffset() == dt.timedelta(0)
            snapshot = dj_absurd.get_result(result.id)
            assert snapshot is not None
            assert snapshot.enqueued_at == FROZEN
    finally:
        with conn.cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL("alter database {} reset timezone").format(
                    psycopg.sql.Identifier(dbname)
                )
            )
        conn.close()


@pytest.mark.parametrize(
    "bad_instant",
    [FROZEN.replace(tzinfo=None), "2026-01-01T12:00:00+00:00"],
    ids=["naive-datetime", "string"],
)
@pytest.mark.parametrize("mover", ["freeze_time", "move_to"])
def test_every_clock_entrypoint_needs_an_aware_datetime(
    bad_instant: dt.datetime | str, dj_absurd: AbsurdTestRuntime, mover: str
) -> None:
    with contextlib.ExitStack() as stack:
        # Entering IS the call for freeze_time, so both entrypoints reduce to one
        # "hand it the instant" callable and the rule is asserted once per case.
        def enter_freeze(instant: dt.datetime) -> None:
            stack.enter_context(dj_absurd.freeze_time(instant))

        move: t.Callable[[dt.datetime], None] = (
            enter_freeze
            if mover == "freeze_time"
            else stack.enter_context(dj_absurd.freeze_time(FROZEN)).move_to
        )

        with pytest.raises(TypeError, match=NAIVE_INSTANT_MESSAGE):
            move(bad_instant)  # type: ignore[arg-type]


def test_freezing_without_time_machine_installed_raises(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """A real import condition, not a patched attribute: the module is uncached and a
    finder ahead of every other one refuses to supply it.

    The finder is installed around the single ``freeze_time()`` entry and removed again,
    and the only import that entry makes is ``time_machine`` (everything else it touches
    is already cached), so it can refuse unconditionally and name whatever it was asked
    for. The module-level ``import time_machine`` is what makes the uncache-and-restore
    exact rather than conditional.
    """

    class BlockTimeMachine(importlib.abc.MetaPathFinder):
        def find_spec(
            self,
            fullname: str,
            path: t.Sequence[str] | None = None,
            target: object | None = None,
        ) -> importlib.machinery.ModuleSpec | None:
            msg = f"blocked for this test: {fullname}"
            raise ImportError(msg)

    blocker = BlockTimeMachine()
    del sys.modules[time_machine.__name__]
    sys.meta_path.insert(0, blocker)
    try:
        with (
            pytest.raises(
                ImproperlyConfigured,
                match=(
                    r"django-absurd: freezing durable time needs the time-machine "
                    r"package\. Install it in your test environment: pip install "
                    r"time-machine\."
                ),
            ),
            contextlib.ExitStack() as stack,
        ):
            stack.enter_context(dj_absurd.freeze_time())
    finally:
        sys.meta_path.remove(blocker)
        sys.modules[time_machine.__name__] = time_machine
