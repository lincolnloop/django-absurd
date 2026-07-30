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

``AbsurdTestRuntime`` (returned by the ``dj_absurd`` pytest fixture in
``django_absurd.pytest_plugin``) is the read/drain/clock facade for durable tests:
``sync_queues``, ``get_result``, ``drain``, ``emit``, the transaction guard all four
share, plus clock control (``freeze_time``, which yields a ``FrozenTime``, and
``now``). Every other
METHOD on that class is an internal and carries a leading underscore; its dataclass
fields are plain state, not part of the public surface.

**Import-safety constraint**: ``django_absurd.pytest_plugin.pytest_configure`` imports
this module on EVERY pytest run in ANY venv that has django-absurd installed — Django
project or not — so this module's top level must stay settings-free. Everything
imported above is: the one import that would read ``INSTALLED_APPS`` is
``django_absurd.models``, and it is confined to ``queues.reconcile_queue``'s own
function body (see the comment there). Never add a module-level import here that
reaches ``django_absurd.models``, ``.checks`` or ``.admin`` — the run would die with
``INTERNALERROR: ImproperlyConfigured`` before collecting anything, in every non-Django
project in the venv. ``tests/core/test_pytest_plugin.py``'s
``test_a_pytest_run_with_no_django_settings_still_collects`` guards exactly that.
"""

import datetime as dt
import functools
import typing as t
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import psycopg.sql
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
)
from django_absurd.params import NOT_SET, NotSet

if t.TYPE_CHECKING:
    import time_machine  # dev/test-only dependency, imported lazily at runtime
    from absurd_sdk import JsonValue

CLEANUP_MARKER = "absurd_cleanup_installed"


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

    Caveats, measured (see the design doc for the full reasoning):

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
    conflating the two is what makes ``TaskSnapshot`` unable to express an in-flight
    retry (see its own caveats). Per-run state is read right after THAT run's own
    execution, never batched at drain end: a run that suspends (``sleeping``) or fails
    (``failed``, with its own ``failure``) reports honestly, and a retry sequence reads
    attempt-by-attempt instead of collapsing to one final verdict.

    The same ``run_id`` can legitimately appear twice in one drain: an ``await_event``
    waiter re-arms its own run, so it first appears ``sleeping`` and later
    ``completed``.
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
    """The handle ``AbsurdTestRuntime.freeze_time`` yields: durable time's two movers.

    ``move_to``/``shift`` are time-machine's own ``Coordinates`` vocabulary, and the
    absence of a ``travel`` is deliberate — travelling implies ticking, while both
    halves of this clock are frozen: ``absurd.fake_now`` is a static literal, so
    Postgres never ticks, and a ticking Python clock would drift out of lockstep with
    it.

    ``move_to`` and ``shift`` are the whole surface. ``_apply_move`` behind them is the
    window's own two-clock apply, bound when the freeze opened, so the handle carries no
    clock state of its own — the runtime stays the single owner of what has to be
    released. It also holds the window open/closed, so BOTH movers raise once the block
    has exited rather than silently re-freezing real time behind the test's back.
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

        That same reading is also passed through as the direction reference, so
        ``_apply_clock_move`` decides forward-vs-backward against the exact ``now()``
        this method took, not a second, later one of its own.

        Valid only while the ``freeze_time`` block that yielded this handle is open.
        """
        now = dt.datetime.now(dt.UTC)
        self._apply_move(now + delta, now)


@dataclass
class AbsurdTestRuntime:
    """Read/drain/clock facade returned by the ``dj_absurd`` pytest fixture.

    Holds the Django database alias the Absurd backend runs on, and — for as long as a
    ``freeze_time`` block is open — the time-machine coordinate driving Python's half of
    the clock. Every per-queue method resolves its queue name from its own argument,
    defaulting to ``"default"`` — except ``get_result``, which prefers a prefixed id's
    own queue over an unpassed ``queue`` argument (see its own docstring), and
    ``sync_queues``, which is whole-catalog and takes no queue at all.

    Public surface: ``freeze_time``, ``now``, ``sync_queues``, ``drain``, ``emit``,
    ``get_result``.

    ``time_travel`` is what release stops; ``traveller`` is what later moves take. Both
    stay ``None`` outside a ``freeze_time`` block, which is how a drain-only test pays
    nothing and cannot leak a frozen clock, and a live ``time_travel`` is what makes a
    nested freeze detectable.
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
        """Provision every queue declared on the Absurd backend — the runtime
        counterpart of ``manage.py absurd_sync_queues``.

        **Rarely needed, so don't reach for it defensively.** django-absurd provisions
        the declared catalog from its own ``post_migrate`` receiver, and pytest-django
        migrates the test database, so a test that enqueues on a declared queue already
        has its tables. What needs this is a test that CHANGED the topology: a
        ``settings`` override declaring a queue the migration never saw, or a fixture
        that dropped the queues to isolate itself.

        Whole-catalog, hence no ``queue`` argument — provisioning also rebuilds the
        admin views over the FULL catalog, which reconciling one queue cannot express.

        Transaction-guarded like the reads, for a different reason: ``create_queue`` is
        DDL, so a sync inside an open transaction is invisible to the worker's own
        connection and is rolled back with the test — provisioning that looks like it
        happened and didn't.
        """
        guard_against_open_transaction(self.alias, "sync_queues")
        backend = queues.get_absurd_backend()
        if backend is None:
            raise BackendNotConfiguredError(0)
        queues.provision_backend(backend)

    def get_result(
        self, task_id: str | uuid.UUID, queue: str | NotSet = NOT_SET
    ) -> TaskSnapshot | None:
        """Look up one task by id, returning ``None`` if it doesn't exist.

        ``task_id`` accepts either a Django ``TaskResult.id`` (``"queue:uuid"``) or a
        bare uuid. A ``"queue:uuid"`` id's own prefix is HONOURED as the queue to
        query when ``queue`` is left unpassed. ``queue`` defaults to the ``NOT_SET``
        sentinel rather than the literal string ``"default"`` precisely so an
        EXPLICITLY passed ``queue=`` — including ``queue="default"`` — is
        distinguishable from an omitted one: pass one that disagrees with a prefixed
        id's own queue and this raises ``TaskIdQueueMismatchError`` naming both,
        instead of silently picking one. A bare uuid (no prefix) resolves ``queue`` to
        ``"default"`` when left unpassed, same as always.

        A declared-but-unprovisioned queue raises ``QueueNotProvisionedError`` —
        the same facade ``drain()`` and ``emit()`` give that condition, rather than
        Django's own ``ProgrammingError`` leaking through this one read. The ORM
        wraps the underlying ``psycopg.errors.UndefinedTable``, so the classifier
        reads it off ``exc.__cause__``, not the wrapper. Only a relation of THIS
        queue's own earns the translation; an unrelated missing relation re-raises
        as itself, chained, same as ``drain_queue``/``emit_event``.
        """
        guard_against_open_transaction(self.alias, "get_result")
        raw_task_id = str(task_id)
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
            task, run = read_task_and_last_run(self.alias, resolved_queue, raw_task_id)
        except ProgrammingError as exc:
            cause = exc.__cause__
            if not isinstance(
                cause, psycopg.errors.UndefinedTable
            ) or not queues.names_a_queue_table(cause, resolved_queue):
                raise
            raise QueueNotProvisionedError(resolved_queue) from exc
        if task is None:
            return None
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
        emit_event(name, payload, queue=queue)

    def drain(self, queue: str = "default") -> list[RunSnapshot]:
        """Burst-drain ``queue`` synchronously, returning one ``RunSnapshot`` per run
        executed, in claim order.

        A run that suspended (durable sleep, ``await_event``) is still returned, with
        ``state="sleeping"`` — the honest reading. The same run can appear twice: an
        ``await_event`` waiter re-arms its own run when a same-drain emit wakes it, so
        it first appears ``sleeping`` then ``completed``. Spawned children run in the
        SAME drain (the burst loop claims until nothing is claimable), so they appear
        too. ``drain() == []`` does not mean nothing happened — cancellation rules run
        inside claiming itself and produce no claim row; use ``get_result`` for those.

        The worker import stays in-function for COST and CONTAINMENT, not import-safety
        (it is settings-free, verified): ``pytest_configure`` imports THIS module on
        every pytest run in any venv with django-absurd installed, and
        ``django_absurd.worker`` is the runtime execution engine — asyncio bridge,
        thread pool, signal handling, and Django 6's brand-new ``django.tasks``. Only
        ``drain()`` needs any of it, so a project that never drains should not load it
        at pytest bootstrap, and a future ``django.tasks`` that reads settings at import
        cannot break unrelated projects' test runs from here.
        """
        guard_against_open_transaction(self.alias, "drain")
        from django_absurd import worker  # noqa: PLC0415

        drained = worker.drain_queue(queue)
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

            An escaped handle would otherwise re-freeze silently — the first move after
            the block starts a FRESH travel from real now and rewrites the GUC, cleaned
            up invisibly by the fixture's crash net. That is the exact failure shape
            this whole facade exists to convert into a loud one.

            ``reference`` passes through the SAME ``now()`` reading a caller already
            took to build ``dest`` (a bare entry, ``shift()``), so direction is decided
            against that one reading rather than a second, later one. ``None`` means
            the caller had no such reading to reuse (an explicit ``move_to()``/entry
            instant isn't derived from ``now()`` at all), and ``_apply_clock_move``
            takes its own single reading in that case.
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
        self, when: dt.datetime, reference: dt.datetime | None = None
    ) -> None:
        """Move BOTH clocks to ``when``, ordered by DIRECTION so an interrupted move
        always lands ahead-on-Python — the benign side — never ahead-on-Postgres.

        Postgres ahead of Python is the one unrecoverable direction: the run is
        claimed, the SDK re-checks the wake on replay and re-suspends, and the burst
        drain re-arms the same wake forever. Python ahead of Postgres just leaves a
        sleeping run unclaimed — a merely-incomplete move, not a deadlock. Which order
        lands which side ahead flips with the direction of the move itself:

        - FORWARD (``when`` at or after the reference instant — the entry freeze to
          real now, and every ``shift``): Python moves first, Postgres second. A
          failure in between leaves Python ahead — benign. EQUAL counts as forward, so
          a bare ``freeze_time()`` entry always takes this branch.
        - BACKWARD (``when`` strictly before the reference instant — a ``move_to``
          earlier than the current instant, entry to an explicit past instant
          included): moving Python first would invert it — Postgres, left at the OLDER
          instant, would end up ahead once Python jumped back. So Postgres moves first
          here, Python second, keeping a failure in between on the same benign side.

        ``reference`` is a SINGLE ``now()`` reading, taken once by whichever caller
        needed one to build ``when`` in the first place (a bare entry, ``shift()``) and
        passed straight through — never re-read here. A second, later read would let
        direction resolve by OS clock resolution instead of intent: at a bare entry,
        ``when`` and the comparison instant would be built from two independent
        ``now()`` calls, tying or not by chance rather than by rule. ``None`` means the
        caller had no such reading to reuse (an explicit ``move_to()``/entry instant
        isn't derived from ``now()`` at all), so this takes its own single reading —
        still only one read total for that call.

        Both validation arms matter. A non-``datetime`` would otherwise reach
        ``astimezone`` and die with an unhelpful ``AttributeError``, and a string that
        survived to the GUC is accepted by ``ALTER DATABASE`` only to explode inside
        ``absurd.current_time()`` on every NEW session — far from the call that caused
        it. A naive ``datetime`` is worse than useless: ``absurd.current_time()`` casts
        the GUC text using the READING session's ``TimeZone``, and the worker's
        connection inherits the server default, so on a server west of UTC a naive
        instant puts Postgres ahead of Python — the deadlock direction.
        """
        if not isinstance(when, dt.datetime) or when.utcoffset() is None:
            msg = (
                "django-absurd: freeze_time() and move_to() need a timezone-aware "
                "datetime; a naive one is ambiguous and would desynchronise Postgres "
                "from Python. Pass tzinfo=datetime.UTC (or any zone)."
            )
            raise TypeError(msg)
        guard_against_blocked_database(self.alias)
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

        time-machine is a test-time dependency of the PROJECT UNDER TEST, never bundled
        and never an extra, so the import is lazy and its failure is translated: a
        ``drain``-only test must not need the package, and Python caches the module so
        only the first move pays for it.

        ``tick=False`` is a correctness requirement: ``absurd.fake_now`` is a static
        literal, so Postgres never ticks, and a ticking Python clock would drift out of
        lockstep with it.

        Both fields are assigned only once ``start()`` has returned, so a failed start
        leaves nothing for the release to ``stop()``.
        """
        try:
            import time_machine  # noqa: PLC0415
        except ImportError as err:
            msg = (
                "django-absurd: freezing durable time needs the time-machine package. "
                "Install it in your test environment: pip install time-machine."
            )
            raise ImproperlyConfigured(msg) from err

        if self.traveller is None:
            time_travel = time_machine.travel(instant, tick=False)
            traveller = time_travel.start()
            self.time_travel, self.traveller = time_travel, traveller
        else:
            self.traveller.move_to(instant)

    def _write_fake_now(self, instant: dt.datetime) -> None:
        """Set ``absurd.fake_now`` at DATABASE level, then on Django's live session.

        Database level because the burst worker opens its OWN connection per drain and
        only a database default reaches it; session level as well because a database
        default reaches only NEW sessions, so an ``enqueue()`` on Django's already-open
        connection would keep stamping real time.

        ``ALTER DATABASE`` rejects bind parameters, hence the composed literal — and the
        literal always carries its UTC offset, so the cast inside
        ``absurd.current_time()`` cannot depend on the reading session's ``TimeZone``.
        The database name is read at runtime, so xdist's per-worker databases work.

        ``_apply_clock_move`` has already run ``guard_against_blocked_database`` before
        either clock moves, so a test with no earned DB access never reaches this
        method's own raw connection at all.
        """
        statement = psycopg.sql.SQL(
            "alter database {name} set absurd.fake_now = {instant}"
        ).format(
            name=psycopg.sql.Identifier(connections[self.alias].settings_dict["NAME"]),
            instant=psycopg.sql.Literal(instant.isoformat()),
        )
        with open_test_connection(self.alias) as cursor:
            cursor.execute(statement)
        with connections[self.alias].cursor() as session_cursor:
            session_cursor.execute(
                "select set_config('absurd.fake_now', %s, false)",
                [instant.isoformat()],
            )

    def _release_clock(self) -> None:
        """Restore real time on both clocks — every ``freeze_time`` block's own exit,
        and the fixture teardown behind a test that never reached it.

        A runtime with no open freeze touches nothing, which is what makes the two
        callers safe to chain: whichever runs first releases, the other returns. The
        Postgres half is released even if stopping time-machine raises — a stopped
        Python clock over a live ``absurd.fake_now`` is the deadlock direction, and
        unlike the Python half the GUC outlives the process that set it.
        """
        if self.time_travel is None:
            return
        time_travel, self.time_travel, self.traveller = self.time_travel, None, None
        try:
            time_travel.stop()
        finally:
            self._reset_fake_now()

    def _reset_fake_now(self) -> None:
        """Unset ``absurd.fake_now`` at database level, then on Django's session.

        The database-level half is ``django_absurd.flush.reset_fake_now``, the single
        implementation of that targeted ``RESET`` (never ``RESET ALL``), shared with the
        post-test flush and the session-start sweep. The session-level half is a
        different statement on a different connection — Django's own live session, which
        a database-level default never reaches.
        """
        flush.reset_fake_now(self.alias)
        with connections[self.alias].cursor() as session_cursor:
            session_cursor.execute("reset absurd.fake_now")


def read_task_and_last_run(
    alias: str, queue: str, task_id: str
) -> "tuple[backends.TaskModel | None, backends.RunModel | None]":
    """Read ``queue``'s task row and its last attempt's run, through Django's ORM.

    The same per-queue dynamic models the production read uses
    (``backends.fetch_task_and_run``), so the two cannot drift on column names or on
    jsonb/timestamptz decoding — and no hand-written SQL to keep in step with Absurd's
    own schema. Not ``fetch_task_and_run`` itself: that one raises
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
    """Open a DEDICATED, short-lived, UTC-pinned connection for a test read.

    Built from ``get_connection_params()`` with BOTH ``cursor_factory`` and
    ``context`` dropped — ``context`` carries Django's psycopg adapters, whose
    timestamptz loader relabels rather than converts (``replace(tzinfo=...)``), which
    is only correct when the session's ``TimeZone`` is already UTC. Inherited unpinned
    on a non-UTC server it silently yields wall-clock digits mislabeled UTC — a
    different instant. So the session is pinned to UTC itself right after connecting,
    making every value read back through this connection UTC-aware by construction.

    Not Django's own connection: a held connection never sees a database-level GUC
    default applied after it opened, and a fresh one is needed on every read anyway
    (mirrors ``django_absurd/connection.py:open_central_connection``).

    Every remaining caller reads the CLOCK or writes the GUC that fakes it — ``now``,
    ``_write_fake_now``, and the GUC oracles in ``tests/utils.py``. Nothing here reads a
    jsonb column any more (task and run state come through the ORM), which is why this
    registers no json loader of its own.
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

    Checked at CALL time, not fixture setup — a plain ``db`` test, a legitimate
    ``django_db(transaction=True)`` test that happens to call from inside
    ``transaction.atomic()``, a ``transactional_db``-only test, and a class-based
    ``TestCase`` are all covered by ``in_atomic_block``, which a marker/fixture-name
    check would miss. Uncommitted rows in an open transaction are invisible to
    Absurd's own connection (worker, or this module's own fresh reads), so the
    alternative is a confusing, silent wrong answer instead of a loud one.
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
    before any raw psycopg connection gets a chance to run. Forcing ``django_db_setup``
    ourselves would not be enough: pytest-django computes which aliases to provision
    from the markers across the WHOLE collected session, so a session with no OTHER
    ``django_db``-marked test touching ``alias`` would leave it unswapped from the live
    settings database no matter how eagerly this fixture asked for setup. Reusing
    pytest-django's own per-test block instead catches every such session shape, not
    just the common one.
    """
    try:
        connections[alias].ensure_connection()
    except RuntimeError as exc:
        msg = (
            "django-absurd: freeze_time() needs real Django database access to pin "
            f"Postgres's clock on '{alias}', and this test has none. Mark it "
            "@pytest.mark.django_db(transaction=True)."
        )
        raise RuntimeError(msg) from exc
