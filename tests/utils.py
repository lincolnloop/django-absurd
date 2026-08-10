import contextlib
import os
import signal
import threading
import time
import typing as t
import uuid

import psycopg
import psycopg.sql
from absurd_sdk import Absurd, CreateQueueOptions
from django.core.management import call_command
from django.db import connections
from django.dispatch import Signal

from django_absurd import worker
from django_absurd.test import open_test_connection

if t.TYPE_CHECKING:
    from collections.abc import Mapping

    from django.tasks import TaskResult, TaskResultStatus

ABSURD_BACKEND = "django_absurd.backends.AbsurdBackend"

# tests/tasks.py declares @task(queue_name="other") and @task(queue_name="reports")
# at module level; importing any task from that module validates those queue names
# against the current backend. Any test that imports from tests.tasks (directly or
# transitively) must therefore declare at least "other" and "reports" alongside
# "default" — this is why make_tasks_settings() defaults to all three.
DECLARED_QUEUES: dict[str, CreateQueueOptions] = {
    "default": {},
    "other": {},
    "reports": {},
}


class HasContent(t.Protocol):
    """What parse_html()/rows() actually need — matches both django.http.HttpResponse
    and the test client's private ``_MonkeyPatchedWSGIResponse``."""

    content: bytes


def make_tasks_settings(
    queues: "Mapping[str, CreateQueueOptions] | None" = None,
    schedule: "Mapping[str, dict[str, object]] | None" = None,
    cleanup: dict[str, str] | None = None,
    database: str | None = None,
    default_max_attempts: int | None = None,
) -> dict[str, dict[str, t.Any]]:
    """Build a ``settings.TASKS`` dict for the AbsurdBackend.

    ``queues`` defaults to ``DECLARED_QUEUES`` (default/other/reports); pass an
    override for tests exercising a different catalog (e.g. an undeclared queue).
    """
    options: dict[str, t.Any] = {
        "QUEUES": dict(DECLARED_QUEUES if queues is None else queues),
    }
    if schedule is not None:
        options["SCHEDULE"] = schedule
    if cleanup is not None:
        options["CLEANUP"] = cleanup
    if database is not None:
        options["DATABASE"] = database
    if default_max_attempts is not None:
        options["DEFAULT_MAX_ATTEMPTS"] = default_max_attempts
    return {"default": {"BACKEND": ABSURD_BACKEND, "OPTIONS": options}}


def run_absurd_worker(queue: str = "default") -> None:
    worker.drain_queue(queue)


def start_worker_until_done(
    is_done: t.Callable[[], bool],
    *,
    timeout: float = 5.0,
    **options: t.Any,
) -> None:
    """Run ``absurd_worker`` to completion, stopping it once ``is_done()`` holds.

    ``is_done`` gates on real work — a row the task wrote, a beat firing — so this is
    for tests asserting an OUTCOME of running the worker, not just that it started.

    The command runs in the calling thread so ``capsys``/``caplog`` see it; a watcher
    thread fires the SIGTERM the worker's own signal handler turns into a graceful
    stop, and only ever while that handler is the one installed — see
    ``stop_handler_is_installed``. A command that errors out before the worker loop
    gets no signal at all.

    The stop lives in a ``finally``, so a predicate that raises still stops the worker
    (the exception then surfaces through pytest's unhandled-thread-exception hook)
    rather than leaving the command running with nothing left to end it. ``timeout``
    stays under the suite's per-test cap so an unreachable predicate stops the worker
    and fails its own test, instead of the cap firing first.
    """
    previous_handler = signal.getsignal(signal.SIGTERM)
    returned = threading.Event()

    def stop_once_done() -> None:
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:  # pragma: no branch
                if stop_handler_is_installed(previous_handler) and is_done():
                    break
                if returned.wait(0.05):
                    break
        finally:
            if not returned.is_set() and stop_handler_is_installed(previous_handler):
                os.kill(os.getpid(), signal.SIGTERM)
            # Thread-local, so this closes only the connection the predicate's ORM
            # read opened on this thread.
            connections.close_all()

    watcher = threading.Thread(target=stop_once_done, daemon=True)
    watcher.start()
    try:
        call_command("absurd_worker", **options)
    finally:
        returned.set()
        watcher.join(timeout=5)


def start_worker(*, timeout: float = 5.0, **options: t.Any) -> None:
    """Run ``absurd_worker`` just long enough to see its signal handler installed, then
    stop it — for tests asserting something that happens before the worker loop ever
    claims a run: the provisioning report, the startup banner, the logging handler the
    command attaches. Nothing about the worker's actual work is waited on; a test that
    needs a row or a beat to have landed wants ``start_worker_until_done`` instead.

    Delegates to ``start_worker_until_done`` with an always-true predicate so there is
    one implementation of the watcher thread, the handler-installed guard, and the
    thread-local ``connections.close_all()``.
    """
    start_worker_until_done(lambda: True, timeout=timeout, **options)


def stop_handler_is_installed(previous_handler: object) -> bool:
    """Whether the SIGTERM handler currently installed is the worker's own.

    Anything else means a signal would land somewhere that is not a graceful stop:
    whatever pytest had installed before the command reached the worker loop, or
    Python's default — which terminates the session outright — after asyncio removes
    the worker's handler on the way out. Both are exactly what a stray SIGTERM from a
    watcher thread must not hit.
    """
    handler = signal.getsignal(signal.SIGTERM)
    return handler is not previous_handler and callable(handler)


def claim_one_run(queue: str = "default", *, claim_timeout: int) -> uuid.UUID:
    """Take a lease on one run without executing it, returning its run_id.

    Leaves the run ``running`` with a ``claim_expires_at`` ``claim_timeout`` seconds
    out, so advancing durable time past that lease lets the ``$ClaimTimeout`` sweep
    inside the next ``claim_task`` expire and re-arm it.
    """
    conn = psycopg.connect(**get_absurd_connection_params(), autocommit=True)
    try:
        claimed = Absurd(conn, queue_name=queue).claim_tasks(
            claim_timeout=claim_timeout
        )
        return uuid.UUID(str(claimed[0]["run_id"]))
    finally:
        conn.close()


def heartbeat_one_run(
    run_id: uuid.UUID, queue: str = "default", *, seconds: int
) -> None:
    """Extend a claimed run's lease by ``seconds`` from now, via the same
    ``absurd.extend_claim`` RPC ``TaskContext.heartbeat()`` calls.

    A run claimed manually by ``claim_one_run`` (rather than actually executing) has
    no live ``TaskContext`` to heartbeat through, so this calls the SQL function
    directly on a fresh connection instead.
    """
    conn = psycopg.connect(**get_absurd_connection_params(), autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "select absurd.extend_claim(%s, %s, %s)", (queue, run_id, seconds)
            )
    finally:
        conn.close()


def get_absurd_connection_params() -> dict[str, t.Any]:
    """Django's own connection params for the Absurd database, for a raw psycopg client.

    Anything reaching Absurd outside Django's connection — a manual claim, a heartbeat
    on a claimed run — needs the database THIS session provisioned, and only Django
    knows which that is (pytest-django swaps ``NAME`` per run, per xdist worker).
    """
    params: dict[str, t.Any] = connections["default"].get_connection_params()
    return params


@contextlib.contextmanager
def hide_absurd_schema() -> t.Iterator[None]:
    """Make the Absurd schema unreachable inside the block, then restore it.

    A rename, not a drop: nothing is destroyed, so the schema comes back with its
    functions, tables and rows intact and Django's migration records stay true the whole
    time. Unapplying the migrations would be the other way to get here and is not
    available — a delta migration carries no downgrade SQL, so the chain is irreversible
    by design (see ``django_absurd/migrations/0002_absurd_0_5_0.py``).
    """
    with connections["default"].cursor() as cursor:
        cursor.execute("ALTER SCHEMA absurd RENAME TO absurd_hidden")
    try:
        yield
    finally:
        with connections["default"].cursor() as cursor:
            cursor.execute("ALTER SCHEMA absurd_hidden RENAME TO absurd")


def set_database_fake_now(value: str) -> None:
    """Plant a database-level ``absurd.fake_now``, as a killed frozen test would leave.

    ``ALTER DATABASE`` rejects bind parameters, hence the composed literal. The database
    name comes from the settings dict at runtime so an xdist worker plants it on its own
    test database.
    """
    statement = psycopg.sql.SQL(
        "alter database {name} set absurd.fake_now = {value}"
    ).format(
        name=psycopg.sql.Identifier(connections["default"].settings_dict["NAME"]),
        value=psycopg.sql.Literal(value),
    )
    with open_test_connection("default") as cursor:
        cursor.execute(statement)


def read_database_fake_now() -> str | None:
    """Return the database-level ``absurd.fake_now``, or ``None`` when it is unset.

    Read from the catalog rather than from a session, so it reports what a NEW
    connection would inherit — the thing that outlives a killed run.
    """
    with open_test_connection("default") as cursor:
        cursor.execute(
            "select split_part(cfg, '=', 2) from pg_db_role_setting s "
            "join pg_database d on d.oid = s.setdatabase "
            "cross join unnest(s.setconfig) as cfg "
            "where d.datname = %s and cfg like 'absurd.fake_now=%%'",
            [connections["default"].settings_dict["NAME"]],
        )
        row = cursor.fetchone()
    return None if row is None else str(row[0])


def read_session_fake_now() -> str:
    """``absurd.fake_now`` as Django's own open session sees it — the session-level
    twin of ``read_database_fake_now``, what an ``enqueue()`` stamps a task with. Reads
    back as an empty string once RESET, never as NULL.
    """
    with connections["default"].cursor() as cursor:
        cursor.execute("select current_setting('absurd.fake_now', true)")
        return str(cursor.fetchone()[0])


def reset_database_fake_now() -> None:
    """Clear the database-level ``absurd.fake_now`` — cleanup half of planting one."""
    statement = psycopg.sql.SQL("alter database {name} reset absurd.fake_now").format(
        name=psycopg.sql.Identifier(connections["default"].settings_dict["NAME"])
    )
    with open_test_connection("default") as cursor:
        cursor.execute(statement)


class RecordingReceiver:
    """Collects TaskResults from a signal.

    Thread-safe because the enqueue seam sends on whatever thread called it, so a sync
    task body enqueueing at concurrency>1 reaches it from several pool threads at once
    and a bare list would race. A class rather than a closure so a test can hold one
    object and read it after the block.

    ``statuses`` exists because one ``TaskResult`` is MUTATED through a task's whole
    lifecycle and handed to every signal. That is Django's own semantics, not ours
    (``ImmediateBackend._execute_task`` mutates one result in place from READY through
    to its final status), so
    ``results[0].status`` read after the block is the task's FINAL status, not the
    status when that signal fired. Assert transient state through ``statuses``, which
    snapshots it at receive time.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.received: list[TaskResult[t.Any, t.Any]] = []
        self.seen_statuses: list[TaskResultStatus] = []

    def __call__(
        self, sender: type, task_result: "TaskResult[t.Any, t.Any]", **kwargs: t.Any
    ) -> None:
        with self.lock:
            self.received.append(task_result)
            self.seen_statuses.append(task_result.status)

    @property
    def results(self) -> list["TaskResult[t.Any, t.Any]"]:
        with self.lock:
            return list(self.received)

    @property
    def statuses(self) -> list["TaskResultStatus"]:
        with self.lock:
            return list(self.seen_statuses)


@contextlib.contextmanager
def connect_receiver(
    signal: Signal, receiver: t.Any, *, sender: type
) -> t.Iterator[None]:
    """Connect for the duration of the block, always disconnecting.

    connect() sits inside the try so a failure anywhere after it still disconnects; a
    receiver leaked here fires for every later test in the same process. weak=False
    because Signal.connect otherwise holds a weak reference and a receiver the caller
    does not keep alive can be collected mid-test, silently never firing.
    """
    try:
        signal.connect(receiver, sender=sender, weak=False)
        yield
    finally:
        signal.disconnect(receiver, sender=sender)
