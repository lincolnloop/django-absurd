"""Auto Absurd state cleanup for Django's test runner, plus the ``dj_absurd`` test
fixture.

``install_absurd_cleanup()`` monkeypatches ``TransactionTestCase._post_teardown`` so
that every DB-backed test case flushes leftover Absurd state (per-queue tables,
scheduled jobs) after it runs. Patching that hook IS the detection: ``_post_teardown``
is defined on ``TransactionTestCase`` (``TestCase`` inherits it) and fires only for DB
test cases, and it runs inside pytest-django's ``django_db_blocker.unblock()`` context,
so the flush executes while the DB is unblocked.

The project's no-monkeypatch rule governs test-code hygiene; this is library test-infra
integration (pytest-django itself patches ``BaseDatabaseWrapper.ensure_connection``),
so the patch here is deliberate.

``AbsurdTestRuntime`` (yielded by the ``dj_absurd`` fixture in
``django_absurd.pytest_plugin``) is the read/drain/clock facade for durable tests;
underscore-prefixed methods are internal, and its dataclass fields are plain state.

**Import-safety constraint**: ``pytest_configure`` imports this module on every pytest
run in any venv with django-absurd installed, so its top level must stay settings-free
— never add a module-level import reaching ``django_absurd.models``/``.checks``/
``.admin``. Full constraint and the one sanctioned choke point:
``django_absurd.pytest_plugin``'s module docstring and ``queues.get_queue_object``.
"""

import asyncio
import datetime as dt
import functools
import importlib
import typing as t
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import psycopg.sql
from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import ProgrammingError
from django.test import TestCase, TransactionTestCase

from django_absurd import admin_views, backends, flush, queues
from django_absurd.events import emit_event
from django_absurd.exceptions import (
    BackendNotConfiguredError,
    QueueNotProvisionedError,
    TaskIdQueueMismatchError,
    TaskNotFoundError,
)
from django_absurd.params import NOT_SET, NotSet

if t.TYPE_CHECKING:
    import time_machine  # dev/test-only dependency, imported lazily at runtime
    from absurd_sdk import JsonValue

CLEANUP_MARKER = "absurd_cleanup_installed"

TIME_MACHINE_MISSING_MESSAGE = (
    "django-absurd: freezing durable time needs the time-machine package. Install it "
    "in your test environment: pip install time-machine."
)


def install_absurd_cleanup() -> None:
    """Idempotently wrap ``TransactionTestCase._post_teardown`` with Absurd cleanup.

    Version-guards first: if Django ever stops defining ``_post_teardown`` on
    ``TransactionTestCase`` the patch would silently attach to nothing, so raise loudly
    instead. Installing more than once is a no-op — the wrapper carries a marker
    attribute the next call detects.
    """
    if "_post_teardown" not in vars(TransactionTestCase):  # pragma: no cover
        msg = (
            "django-absurd expected TransactionTestCase._post_teardown to exist so it "
            "could install automatic Absurd state cleanup, but that hook is absent on "
            "this Django version. django-absurd's pytest integration needs to be "
            "updated for this Django release."
        )
        raise RuntimeError(msg)

    original = TransactionTestCase._post_teardown  # type: ignore[attr-defined]  # noqa: SLF001 -- Django exposes no public teardown hook to wrap
    if getattr(original, CLEANUP_MARKER, False):
        return

    @functools.wraps(original)
    def _post_teardown_with_absurd_cleanup(self: TransactionTestCase) -> None:
        original(self)
        flush_absurd_after_teardown(self)

    setattr(_post_teardown_with_absurd_cleanup, CLEANUP_MARKER, True)
    TransactionTestCase._post_teardown = _post_teardown_with_absurd_cleanup  # type: ignore[attr-defined]  # noqa: SLF001 -- Django exposes no public teardown hook to wrap


def flush_absurd_after_teardown(instance: TransactionTestCase) -> None:
    """Flush Absurd state after a test case's own teardown, when it applies.

    Skips when the case is a transactional ``TestCase`` (its rollback already reverts
    everything — the SCOPED ``_databases_support_transactions()`` probes only the case's
    own aliases), when no Absurd backend is configured, or when the Absurd database is
    not among the case's declared ``databases`` (respecting the ``"__all__"`` sentinel).
    """
    if isinstance(instance, TestCase) and instance._databases_support_transactions():  # type: ignore[attr-defined]  # noqa: SLF001 -- mirrors Django's own TestCase._fixture_teardown; no public equivalent
        return

    if not backends.get_absurd_backends():
        return

    databases = instance.databases
    if databases != "__all__" and queues.resolve_absurd_database() not in databases:
        return

    flush.flush_absurd_state()


@dataclass(frozen=True)
class TaskSnapshot:
    """Where one task stands: one row from ``t_<queue>``, left-joined to its last
    attempt's run for ``failure``.

    Caveats, measured:

    - ``attempts`` counts attempts CREATED, not completed — a task retried once (one
      failure, one pending replacement) already reads ``attempts=2`` before the second
      attempt has run.
    - ``state="sleeping"`` covers both a durable sleep and a retry backoff; it cannot
      tell them apart.
    - ``failure`` is populated only when ``last_attempt_run`` still points at the failed
      run (a terminal failure) — mid-backoff it already points at the fresh pending
      run, so ``failure`` reads ``None`` even though the previous attempt raised.
    """

    queue: str
    task_id: uuid.UUID
    task_name: str
    args: list[t.Any]
    kwargs: dict[str, t.Any]
    state: str
    attempts: int
    enqueued_at: dt.datetime
    result: t.Any | None
    failure: t.Any | None


@dataclass(frozen=True)
class RunSnapshot:
    """One run executed during a ``drain()``, in claim order.

    ``drain()`` reports what RUNS did; ``get_result()`` reports where a TASK stands —
    which is why ``TaskSnapshot`` cannot express an in-flight retry (see its
    caveats). State is read right after each run's own execution, never batched at
    drain end, so a retry sequence reads attempt-by-attempt. The same ``run_id`` can
    appear twice: an ``await_event`` waiter re-arms its own run — first ``sleeping``,
    later ``completed``.
    """

    queue: str
    run_id: uuid.UUID
    task_id: uuid.UUID
    task_name: str
    args: list[t.Any]
    kwargs: dict[str, t.Any]
    attempt: int
    state: str
    result: t.Any | None
    failure: t.Any | None


@dataclass(frozen=True)
class FrozenTime:
    """Handle yielded by ``AbsurdTestRuntime.freeze_time``: durable time's only
    movers.

    ``move_to``/``shift`` are time-machine's ``Coordinates`` vocabulary; no ``travel``
    because travelling implies ticking, and ``absurd.fake_now`` is a static literal —
    a ticking Python clock would drift out of lockstep. ``_apply_move`` is the open
    window's two-clock apply: the handle carries no clock state (the runtime owns
    release) and both movers raise once the block has exited rather than silently
    re-freezing real time.
    """

    _apply_move: t.Callable[[dt.datetime, dt.datetime | None], None]

    def move_to(self, dest: dt.datetime) -> None:
        """Move durable time to ``dest``, a timezone-aware ``datetime``.

        Valid only while the ``freeze_time`` block that yielded this handle is open.
        """
        self._apply_move(dest, None)

    def shift(self, delta: dt.timedelta) -> None:
        """Move durable time forward by ``delta`` of ABSOLUTE elapsed time.

        Absolute, not wall-clock: a seven-day shift across a US spring-forward gap
        lands 7 * 24 hours later as an INSTANT, which is the only thing a durable
        deadline is measured in. The destination is read back from ``datetime.now``
        — already the frozen instant while time-machine holds the process clock — so
        there is one source of truth instead of bookkeeping of our own that could
        disagree with it.

        That same reading passes through as the direction reference, so
        ``_apply_clock_move`` decides forward-vs-backward against the exact ``now()``
        this method took.

        Valid only while the ``freeze_time`` block that yielded this handle is open.
        """
        now = dt.datetime.now(dt.UTC)
        self._apply_move(now + delta, now)


@dataclass
class AbsurdTestRuntime:
    """Read/drain/clock facade returned by the ``dj_absurd`` pytest fixture.

    Public surface: ``freeze_time``, ``now``, ``sync_queues``, ``drain``, ``emit``,
    ``get_result``. Every one of them works unchanged — same name, no ``await`` — from
    an ``async def`` test: the blocking Django/Absurd work each one does goes through
    ``run_off_event_loop``, which steps off a running event loop when there is one.
    Holds the Absurd backend's database alias and, while a
    ``freeze_time`` block is open, the time-machine coordinate driving Python's half
    of the clock. ``time_travel``/``traveller`` stay ``None`` outside a freeze — a
    drain-only test pays nothing and cannot leak a frozen clock, and a live
    ``time_travel`` is what makes a nested freeze detectable. ``time_travel`` is what
    release stops; ``traveller`` is what later moves take.
    """

    alias: str
    time_travel: "time_machine.travel | None" = None
    traveller: "time_machine.Traveller | None" = None

    def __enter__(self) -> "t.Self":
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Release a clock a test left frozen — the crash net behind ``freeze_time``.

        ``freeze_time`` releases its own window on the way out, so this only ever has
        work to do when a test died without leaving the block. The ``dj_absurd`` fixture
        drives it by entering the runtime for the test's duration.
        """
        self._release_clock()

    def sync_queues(self) -> None:
        """Provision every declared queue — the runtime counterpart of ``manage.py
        absurd_sync_queues``.

        Rarely needed: migrate already provisions the declared catalog. Reach for it
        only when the test CHANGED topology — a ``settings`` override declaring a
        queue the migration never saw, or a fixture that dropped the queues.
        Whole-catalog (no ``queue`` argument) because provisioning also rebuilds the
        admin views over the full catalog. Transaction-guarded because the DDL would
        be invisible to the worker's connection and rolled back with the test —
        provisioning that looks like it happened and didn't.
        """
        guard_against_open_transaction(self.alias, "sync_queues")
        backend = queues.get_absurd_backend()
        if backend is None:
            raise BackendNotConfiguredError(0)
        run_off_event_loop(functools.partial(queues.provision_backend, backend))

    def get_result(
        self, task_id: str | uuid.UUID, queue: str | NotSet = NOT_SET
    ) -> TaskSnapshot:
        """Look up one task by id, raising ``TaskNotFoundError`` if it doesn't exist.

        ``task_id`` is a Django ``TaskResult.id`` (``"queue:uuid"``) or a bare uuid.
        A prefixed id's own queue wins when ``queue`` is unpassed; the default is the
        ``NOT_SET`` sentinel (not ``"default"``) so an EXPLICITLY passed ``queue=`` —
        even ``queue="default"`` — that disagrees with the prefix raises
        ``TaskIdQueueMismatchError`` instead of silently picking a side. A bare uuid
        resolves an unpassed ``queue`` to ``"default"``.

        A declared-but-unprovisioned queue raises ``QueueNotProvisionedError``, the
        same facade ``drain()``/``emit()`` give it. The ORM wraps the underlying
        ``UndefinedTable``, so the classifier reads ``exc.__cause__``; an unrelated
        missing relation re-raises as itself, chained.
        """
        guard_against_open_transaction(self.alias, "get_result")
        original_task_id = str(task_id)
        raw_task_id = original_task_id
        if ":" in raw_task_id:
            prefix_queue, _, raw_task_id = raw_task_id.rpartition(":")
            if queue is not NOT_SET and queue != prefix_queue:
                raise TaskIdQueueMismatchError(
                    task_id=str(task_id), prefix_queue=prefix_queue, queue=queue
                )
            resolved_queue = prefix_queue
        else:
            resolved_queue = "default" if queue is NOT_SET else queue
        try:
            task, run = run_off_event_loop(
                functools.partial(
                    read_task_and_last_run, self.alias, resolved_queue, raw_task_id
                )
            )
        except ProgrammingError as exc:
            cause = exc.__cause__
            if not isinstance(
                cause, psycopg.errors.UndefinedTable
            ) or not queues.names_a_queue_table(cause, resolved_queue):
                raise
            raise QueueNotProvisionedError(resolved_queue) from exc
        if task is None:
            raise TaskNotFoundError(task_id=original_task_id, queue=resolved_queue)
        args, kwargs = decode_params(task.params)
        return TaskSnapshot(
            queue=resolved_queue,
            task_id=task.task_id,
            task_name=task.task_name,
            args=args,
            kwargs=kwargs,
            state=task.state,
            attempts=task.attempts,
            enqueued_at=task.enqueue_at,
            result=task.completed_payload,
            failure=None if run is None else run.failure_reason,
        )

    def emit(
        self, name: str, payload: "JsonValue | None" = None, queue: str = "default"
    ) -> None:
        """Emit ``name`` on ``queue``, delegating to ``events.emit_event``.

        Resolves a task suspended in ``await_event(name)`` on the next ``drain()`` — it
        does not itself run anything. Prefer this over enqueuing a one-off task that
        calls ``django_absurd.events.emit_event`` from inside a run: it puts the emit on
        the SAME timeline the assertions read, at the point the test chooses.

        Transaction-guarded like the reads: an event written inside an open transaction
        is invisible to the worker's own connection, so the waiting task would simply
        never wake — a silent no-op instead of a loud error.
        """
        guard_against_open_transaction(self.alias, "emit")
        run_off_event_loop(functools.partial(emit_event, name, payload, queue=queue))

    def drain(self, queue: str = "default") -> list[RunSnapshot]:
        """Run every currently-claimable task on ``queue`` to completion, synchronously,
        one at a time, returning one ``RunSnapshot`` per run in claim order.

        A suspended run (durable sleep, ``await_event``) is returned with
        ``state="sleeping"``. The same run can appear twice — an ``await_event``
        waiter re-arms when a same-drain emit wakes it: first ``sleeping``, later
        ``completed``. Spawned children run in the SAME drain. ``drain() == []`` does
        not mean nothing happened — cancellation rules run inside claiming itself and
        produce no claim row; use ``get_result`` for those.

        The worker import is in-function for cost and containment, not import-safety
        (verified settings-free): only ``drain()`` needs the execution engine, so
        pytest bootstrap in a non-draining project never loads the absurd SDK's async
        client or ``django.tasks``.
        """
        guard_against_open_transaction(self.alias, "drain")
        from django_absurd import worker  # noqa: PLC0415

        drained = run_off_event_loop(functools.partial(worker.drain_queue, queue))
        snapshots: list[RunSnapshot] = []
        for run in drained:
            args, kwargs = decode_params(run.params)
            snapshots.append(
                RunSnapshot(
                    queue=queue,
                    run_id=run.run_id,
                    task_id=run.task_id,
                    task_name=run.task_name,
                    args=args,
                    kwargs=kwargs,
                    attempt=run.attempt,
                    state=run.state,
                    result=run.result,
                    failure=run.failure,
                )
            )
        return snapshots

    @contextmanager
    def freeze_time(self, instant: dt.datetime | None = None) -> t.Iterator[FrozenTime]:
        """Pin BOTH clocks — Python's and Postgres's — for the duration of the block.

        ``instant`` is a timezone-aware ``datetime``, or ``None`` for real now at
        entry. Freeze BEFORE enqueueing: freezing to a PAST instant once rows exist
        leaves their ``available_at`` in the database's future, so nothing is claimable
        until a move passes it.

        The yielded ``FrozenTime`` is the only way to move durable time, and it moves it
        only while this block is open. Leaving the block releases both halves, restoring
        real time, so a test can open several windows in sequence; the fixture's own
        teardown stays as the crash net for a test that dies inside one.

        Nesting raises instead of stacking: two frozen instants cannot both be "now",
        and an inner block's exit would restore real time under the outer one rather
        than the instant it froze.
        """
        if self.time_travel is not None:
            msg = (
                "django-absurd: freeze_time() is already active, and two frozen "
                "instants cannot both be 'now'. Move the open freeze with "
                "move_to()/shift(), or leave its with-block before opening another."
            )
            raise RuntimeError(msg)
        window_open = True

        def move_within_window(
            dest: dt.datetime, reference: dt.datetime | None
        ) -> None:
            """Apply a mover's destination, or refuse once the window has closed.

            An escaped handle would otherwise re-freeze silently — a FRESH travel
            from real now plus a rewritten GUC, cleaned up invisibly by the crash
            net: the exact failure shape this facade exists to make loud.
            ``reference`` is the ``now()`` reading the caller built ``dest`` from
            (entry, ``shift``); ``None`` when ``dest`` wasn't derived from ``now()``.
            """
            if not window_open:
                msg = (
                    "django-absurd: this freeze_time() block has already exited, so "
                    "durable time is real again. Open a new freeze_time() window to "
                    "move durable time again."
                )
                raise RuntimeError(msg)
            self._apply_clock_move(dest, reference)

        try:
            now = dt.datetime.now(dt.UTC)
            if instant is None:
                move_within_window(now, now)
            else:
                move_within_window(instant, None)
            yield FrozenTime(_apply_move=move_within_window)
        finally:
            window_open = False
            self._release_clock()

    @property
    def now(self) -> dt.datetime:
        """Virtual now, UTC-aware, as POSTGRES reports it.

        Read through a fresh UTC-pinned connection: not Django's session (it can hold a
        stale or rolled-back ``SET``, and never sees a database-level default applied
        after it opened) and not Python-side bookkeeping (that would report what we
        meant to apply, so it could never reveal a Python/Postgres desync — the failure
        this most needs visible).
        """
        with open_test_connection(self.alias) as cursor:
            cursor.execute("select absurd.current_time()")
            (instant,) = t.cast("tuple[dt.datetime]", cursor.fetchone())
        return instant

    def _apply_clock_move(
        self, when: dt.datetime, reference: dt.datetime | None
    ) -> None:
        """Move BOTH clocks to ``when``, ordered by direction so an interrupted move
        always lands Python-ahead — the benign side.

        Postgres-ahead is unrecoverable: the run is claimed, the SDK re-suspends on
        replay, and the drain re-arms the same wake forever. Python-ahead just
        leaves a sleeping run unclaimed. Which order lands which side ahead flips with
        the move's direction:

        - FORWARD (``when`` at/after ``reference``; entry and every ``shift``): Python
          first, Postgres second. Equal counts as forward, so a bare ``freeze_time()``
          entry takes this branch.
        - BACKWARD (``when`` strictly before ``reference``): Postgres first — moving
          Python first would leave Postgres at the older instant, i.e. ahead.

        ``reference`` is the caller's single ``now()`` reading, never re-read here — a
        second read would let direction resolve by OS clock resolution instead of
        intent. ``None``: the caller had no reading (explicit ``move_to``/entry
        instant), so take one now.

        Aware-or-raise: a non-``datetime`` would die later in ``astimezone`` or
        explode inside ``absurd.current_time()`` on every NEW session, far from the
        cause. A NAIVE one is worse — the GUC text is cast with the READING session's
        ``TimeZone``, so a server west of UTC lands Postgres-ahead: the deadlock
        direction.

        ``require_time_machine()`` runs before either clock write below — both clocks
        move together or neither does. Without it, the BACKWARD branch would write
        Postgres's GUC first and only discover the missing dependency once
        ``_move_python_clock`` ran, so a project without time-machine would see the
        GUC written and then stranded: ``self.time_travel`` was never assigned (the
        failed ``_move_python_clock`` call raises before either field is set), so
        ``_release_clock`` treats the freeze as never having opened and leaves the
        database-level GUC in place.
        """
        if not isinstance(when, dt.datetime) or when.utcoffset() is None:
            msg = (
                "django-absurd: freeze_time() and move_to() need a timezone-aware "
                "datetime; a naive one is ambiguous and would desynchronise Postgres "
                "from Python. Pass tzinfo=datetime.UTC (or any zone)."
            )
            raise TypeError(msg)
        guard_against_blocked_database(self.alias)
        require_time_machine()
        instant = when.astimezone(dt.UTC)
        current = reference if reference is not None else dt.datetime.now(dt.UTC)
        if instant < current:
            self._write_fake_now(instant)
            self._move_python_clock(instant)
        else:
            self._move_python_clock(instant)
            self._write_fake_now(instant)

    def _move_python_clock(self, instant: dt.datetime) -> None:
        """Hold Python's clock at ``instant`` via time-machine, ``tick=False``.

        The import is lazy because time-machine is optional — a ``drain``-only test
        must not need it. ``require_time_machine()`` has already run, so it can't fail
        here.

        ``tick=False`` is a correctness requirement: ``absurd.fake_now`` is a static
        literal, so Postgres never ticks, and a ticking Python clock would drift out of
        lockstep with it.

        Both fields are assigned only once ``start()`` has returned, so a failed start
        leaves nothing for the release to ``stop()``.
        """
        import time_machine  # noqa: PLC0415

        if self.traveller is None:
            time_travel = time_machine.travel(instant, tick=False)
            traveller = time_travel.start()
            self.time_travel, self.traveller = time_travel, traveller
        else:
            self.traveller.move_to(instant)

    def _write_fake_now(self, instant: dt.datetime) -> None:
        """Set ``absurd.fake_now`` at DATABASE level, then on Django's live session.

        Database level because a drain opens its OWN connection every time —
        only a database default reaches it; session level too because a default
        reaches only NEW sessions, so ``enqueue()`` on Django's already-open
        connection would keep stamping real time. ``ALTER DATABASE`` rejects bind
        parameters, hence the composed literal; it always carries its UTC offset, so
        the cast inside ``absurd.current_time()`` cannot depend on the reading
        session's ``TimeZone``. Name read at runtime for xdist's per-worker
        databases.

        Both writes go through ``run_off_event_loop`` together, so under a running loop
        they land on ONE off-loop connection instead of two. There the session half
        reaches only that off-loop session, which the hop then closes — the sessions an
        ``async def`` test's work actually uses are covered by the hop's own recycling
        of asgiref's thread-sensitive connection, so they reconnect and inherit the
        database-level default just written.
        """
        statement = psycopg.sql.SQL(
            "alter database {name} set absurd.fake_now = {instant}"
        ).format(
            name=psycopg.sql.Identifier(connections[self.alias].settings_dict["NAME"]),
            instant=psycopg.sql.Literal(instant.isoformat()),
        )

        def write_both_clock_levels() -> None:
            with open_test_connection(self.alias) as cursor:
                cursor.execute(statement)
            with connections[self.alias].cursor() as session_cursor:
                session_cursor.execute(
                    "select set_config('absurd.fake_now', %s, false)",
                    [instant.isoformat()],
                )

        run_off_event_loop(write_both_clock_levels)

    def _release_clock(self) -> None:
        """Restore real time on both clocks; a runtime with no open freeze touches
        nothing, so block exit and fixture teardown chain safely.

        The Postgres half is released even if stopping time-machine raises — a
        stopped Python clock over a live ``absurd.fake_now`` is the deadlock
        direction, and unlike the Python half the GUC outlives the process that set
        it.
        """
        if self.time_travel is None:
            return
        time_travel, self.time_travel, self.traveller = self.time_travel, None, None
        try:
            time_travel.stop()
        finally:
            self._reset_fake_now()

    def _reset_fake_now(self) -> None:
        """Unset at database level via ``flush.reset_fake_now`` (the single
        implementation), then on Django's own live session, which a database-level
        default never reaches.

        Paired with ``_write_fake_now``'s hop so the two halves release wherever they
        were set: a ``freeze_time`` block exited inside an ``async def`` test releases
        off the loop, while the fixture's own teardown runs after the loop has closed
        and releases in place.
        """

        def reset_both_clock_levels() -> None:
            flush.reset_fake_now(self.alias)
            with connections[self.alias].cursor() as session_cursor:
                session_cursor.execute("reset absurd.fake_now")

        run_off_event_loop(reset_both_clock_levels)


def read_task_and_last_run(
    alias: str, queue: str, task_id: str
) -> "tuple[backends.TaskModel | None, backends.RunModel | None]":
    """Read ``queue``'s task row and its last attempt's run, through Django's ORM.

    The same per-queue dynamic models the production read uses
    (``backends.fetch_task_and_run``), so the two cannot drift on column names or on
    jsonb/timestamptz decoding. Not ``fetch_task_and_run`` itself: that one raises
    ``TaskResultDoesNotExist`` where this must report a missing task as ``None``, and it
    folds a missing per-queue table into that same error where ``get_result`` lets the
    original missing-relation error surface.

    Two queries rather than one left join, matching the production read: the run is
    needed only for its ``failure_reason``, and reading it separately is what lets the
    ``available_at`` deferral below apply to the run alone.
    """
    tasks_spec = next(s for s in admin_views.ADMIN_ENTITY_SPECS if s.name == "tasks")
    task_model: type[t.Any] = admin_views.build_queue_table_model(tasks_spec, queue)
    task: backends.TaskModel | None = (
        task_model.objects.using(alias).filter(pk=task_id).first()
    )
    if task is None or task.last_attempt_run is None:
        return task, None
    runs_spec = next(s for s in admin_views.ADMIN_ENTITY_SPECS if s.name == "runs")
    run_model: type[t.Any] = admin_views.build_queue_table_model(runs_spec, queue)
    run: backends.RunModel | None = (
        run_model.objects.using(alias)
        .filter(pk=task.last_attempt_run)
        # An indefinite await_event parks a run at Postgres's 'infinity' available_at,
        # which psycopg refuses to decode ("timestamp too large (after year 10K)") —
        # defer it, since a TaskSnapshot never reads it.
        .defer("available_at")
        .first()
    )
    return task, run


def decode_params(params: t.Any) -> tuple[list[t.Any], dict[str, t.Any]]:
    """Split raw ``params`` jsonb into ``(args, kwargs)``, defensively.

    Our own ``enqueue()`` always writes both keys under a mapping, even for a
    zero-arg task, and schedules and task-spawned children take the same path — but a
    queue shared with raw-SDK producers can hold anything (a bare list, proven). Fall
    back to empty rather than raising, or one foreign row takes down the whole read.
    """
    if isinstance(params, Mapping):
        return params.get("args", []), params.get("kwargs", {})
    return [], {}


@contextmanager
def open_test_connection(alias: str) -> t.Iterator[psycopg.Cursor[t.Any]]:
    """Open a dedicated, short-lived, UTC-pinned connection for a test read.

    BOTH ``cursor_factory`` and ``context`` are dropped: ``context`` carries Django's
    psycopg adapters, whose timestamptz loader RELABELS rather than converts
    (``replace(tzinfo=...)``) — correct only when the session zone is UTC, so the
    session is pinned to UTC right after connecting. Not Django's own connection: a
    held session never sees a database-level GUC default applied after it opened, and
    a fresh one is needed per read anyway (mirrors ``connection.py``'s
    ``open_central_connection``). Callers read the clock or write the GUC — nothing
    reads jsonb through this, hence no json loader.
    """
    params: dict[str, t.Any] = connections[alias].get_connection_params()
    params.pop("cursor_factory", None)
    params.pop("context", None)
    conn = psycopg.connect(**params, autocommit=True)
    try:
        with connections[alias].wrap_database_errors, conn.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            yield cursor
    finally:
        conn.close()


def guard_against_open_transaction(alias: str, operation: str) -> None:
    """Raise if called from inside an open transaction on ``alias``.

    Checked at CALL time, not fixture setup — every legitimate call shape is
    covered by ``in_atomic_block``, which a marker/fixture-name check would miss.
    Uncommitted rows in an open transaction are invisible to Absurd's own connection
    (worker, or this module's own fresh reads), so the alternative is a confusing,
    silent wrong answer instead of a loud one.
    """
    if connections[alias].in_atomic_block:
        msg = (
            f"django-absurd: {operation}() ran inside an open transaction, where "
            "uncommitted rows are invisible to Absurd's own connection. Use "
            "@pytest.mark.django_db(transaction=True) and call outside "
            "transaction.atomic()."
        )
        raise RuntimeError(msg)


def guard_against_blocked_database(alias: str) -> None:
    """Raise before ``freeze_time()`` opens its own raw connection, if a test never
    earned real access to ``alias`` in the first place.

    Touches Django's OWN connection first — ``ensure_connection()``, the exact call
    pytest-django's DB blocker patches to refuse a test with no ``django_db`` marker —
    before any raw psycopg connection gets a chance to run. Forcing
    ``django_db_setup`` would not be enough — pytest-django provisions aliases from
    the WHOLE session's markers, so an unmarked-elsewhere session leaves ``alias``
    unswapped. Reusing pytest-django's own per-test block instead catches every such
    session shape, not just the common one.

    Off the loop when there is one, because ``ensure_connection`` is exactly what
    Django's ``async_unsafe`` decorator refuses outright — so under a running loop the
    guard could not run at all on the calling thread. It loses nothing by changing
    threads: BOTH blocks it relies on are patched onto ``BaseDatabaseWrapper`` ITSELF
    (pytest-django's ``_blocking_wrapper``, and Django's undeclared-alias
    ``mock.patch.object``), so they fire for whichever thread's connection asks. The
    alias is re-resolved INSIDE the hop rather than the calling thread's wrapper being
    handed over: connecting stamps ``_thread_ident``, and a test connection first
    opened by the hop thread would then refuse the test's own later cursors.
    """

    def ensure_the_test_can_reach_the_database() -> None:
        connections[alias].ensure_connection()

    try:
        run_off_event_loop(ensure_the_test_can_reach_the_database)
    except RuntimeError as exc:
        msg = (
            "django-absurd: freeze_time() needs real Django database access to pin "
            f"Postgres's clock on '{alias}', and this test has none. Mark it "
            "@pytest.mark.django_db(transaction=True)."
        )
        raise RuntimeError(msg) from exc


def require_time_machine() -> None:
    """Raise ``ImproperlyConfigured`` before either clock moves, if time-machine is not
    installed.

    Runs ahead of both clock writes, so a project without it fails with neither clock
    touched.
    """
    try:
        importlib.import_module("time_machine")
    except ImportError as err:
        raise ImproperlyConfigured(TIME_MACHINE_MISSING_MESSAGE) from err


def run_off_event_loop[T](work: t.Callable[[], T]) -> T:
    """Run ``work`` somewhere Django's synchronous API is legal, and block for its
    result — which is what lets one facade serve both a plain ``def test_`` and an
    ``async def`` one with no second API and no ``await``.

    With no loop running in this thread, ``work`` is simply called: the sync path is
    untouched, same thread, same connection, same traceback. Under a running loop it
    goes to a worker thread, where there IS no running loop, so both Django's
    ``async_unsafe`` guards and ``asyncio.run`` inside the drain are legal
    again. Blocking the loop is safe here because a test is the only thing on it and
    ``work`` never awaits it back — ``drain()``'s ``drain_queue`` builds a loop of its
    own in the worker thread.

    Callers keep their guards on THEIR OWN thread and call this for the DB work only:
    ``in_atomic_block`` is per-connection and ``connections`` is thread-critical, so
    ``guard_against_open_transaction`` evaluated over here would inspect a connection
    the test never used and pass every time. The one guard that does hop is
    ``guard_against_blocked_database``, which cannot run on the calling thread at all
    and keeps its fidelity for the reason given there.

    Two sets of Django connections are closed on the way out, both of them sessions no
    test-runner teardown reaches, and one stranded session is enough to fail teardown's
    ``DROP DATABASE`` (the trap ``worker.aworker_client`` already closes for):

    - the worker thread's own, opened by ``work`` — ``connections`` is thread-critical,
      so these are objects the test itself never saw;
    - asgiref's thread-sensitive session, where ``aenqueue`` and Django's own async ORM
      (``aget``, ``acreate``, …) run their synchronous halves. That one is a
      process-wide thread whose session inherits ``absurd.fake_now`` at CONNECT time,
      and a session-level ``SET`` can only ever reach the thread it runs on — so a
      connection opened before a freeze, or before the last clock move, keeps stamping
      the wrong instant, and does so for every LATER test too. Measured, before this
      close: one preceding ``await Model.objects.acount()`` was enough to make a frozen
      week-long sleep enqueue at real time and never wake. Recycling costs one
      reconnect and leaves the next session to inherit whatever the clock now says.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return work()

    def run_then_close_connections() -> T:
        try:
            return work()
        finally:
            connections.close_all()
            # A loop of this thread's own, so asgiref resolves thread-sensitive work to
            # its process-wide thread rather than back here — the same dispatch
            # ``worker.aworker_client``'s own closing hop already relies on.
            asyncio.run(sync_to_async(connections.close_all)())

    return get_off_loop_executor().submit(run_then_close_connections).result()


@functools.cache
def get_off_loop_executor() -> ThreadPoolExecutor:
    """The one worker thread every ``run_off_event_loop`` hop shares, built on first
    use.

    Cached, not per call or per fixture: a fresh executor per call would leak a thread
    per drain across a suite, and the runtime that owns the fixture is function-scoped,
    so hanging it there would leak one per test instead. One worker is enough because
    each hop blocks until it returns, and a nested call inside the worker thread finds
    no running loop and never queues behind itself.
    """
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="dj-absurd-test")
