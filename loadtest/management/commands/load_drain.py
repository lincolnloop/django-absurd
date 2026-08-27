"""Drain a fixed backlog with a matrix of worker counts and concurrencies.

The worker has only ever been exercised at ``--concurrency 1``. Sync tasks run in a
thread pool that bridges back to the single async engine loop, so the sync arm of this
matrix — several threads per worker, several workers per queue, all claiming from the
same tables — is the point of the probe; the async arm is the control it gets compared
against.

Each cell is one measurement and they run one at a time, never overlapped: two cells
sharing the database would be measuring each other. A cell empties the queue and the
execution log, enqueues ``--tasks`` tasks through the real backend, starts its workers,
and waits for every one of them to exit — each drains the backlog and returns on its
own.

``elapsed_s`` is wall clock around that whole worker phase, so it includes each child's
interpreter start-up and ``django.setup()`` — a fixed cost of
roughly a second, paid once per cell no matter how many workers a cell runs. Cells are
therefore comparable with each other, and small backlogs flatter nobody: at ``--tasks
20`` the start-up is most of the number.

Workers are spawned rather than forked: a forked child inherits this process's open
Postgres connections, and two processes writing one socket is not a throughput
measurement, it is a corruption. ``duplicates`` counts executions beyond the distinct
tasks that produced them. Absurd delivers at least once, so a redelivery is legal and
this number is a finding, not a failure — the failure is a cell that never drained its
backlog at all, which is refused rather than recorded.
"""

import functools
import multiprocessing
import multiprocessing.process
import time
import typing as t

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from django_absurd.flush import truncate_queue_tables
from django_absurd.queues import resolve_absurd_database
from loadtest import clocks, manage, preflight, results, tasks
from loadtest.models import ExecutionLog

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser

RUN_NAME = "drain"
DRAIN_QUEUE = "bulk"
DEFAULT_TASKS = 1_000
DEFAULT_CELLS = ("1x1", "1x4", "4x1", "4x4")
WORKLOADS = ("sync", "async")
DEFAULT_TIMEOUT_SECONDS = 900.0


class Cell(t.NamedTuple):
    label: str
    workers: int
    concurrency: int


class Command(BaseCommand):
    help = (
        "Drain a backlog of tasks with a matrix of worker counts and worker "
        "concurrencies, recording throughput and duplicate executions for each. "
        f"Destructive: every cell empties the '{DRAIN_QUEUE}' queue and the execution "
        "log before it enqueues, so a seeded queue does not survive this command."
    )

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--tasks",
            type=int,
            default=DEFAULT_TASKS,
            help=f"Tasks to enqueue and drain per cell (default: {DEFAULT_TASKS}).",
        )
        parser.add_argument(
            "--cell",
            action="append",
            default=None,
            help=(
                "Cell to measure, written as workers x concurrency (e.g. '4x4'); "
                f"repeatable (default: {' '.join(DEFAULT_CELLS)})."
            ),
        )
        parser.add_argument(
            "--workload",
            choices=(*WORKLOADS, "both"),
            default="both",
            help="Task flavour to drain (default: both).",
        )
        results.add_repeat_argument(parser)
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_TIMEOUT_SECONDS,
            help=(
                "Seconds a cell's workers get to finish before the run is failed "
                f"(default: {DEFAULT_TIMEOUT_SECONDS}). A backlog large enough to "
                "outlast it needs a larger value, not a recorded timing."
            ),
        )

    def handle(self, *args: t.Any, **options: t.Any) -> str:
        preflight.require_migrated_database()
        if options["tasks"] < 1:
            msg = "--tasks must be at least 1."
            raise CommandError(msg)
        cells = [parse_cell(spec) for spec in options["cell"] or list(DEFAULT_CELLS)]
        workloads = (
            WORKLOADS if options["workload"] == "both" else (options["workload"],)
        )

        # Reported cell by cell as well as in the closing table: a matrix that dies on
        # its last cell has still measured every cell before it, and those numbers only
        # survive if they were printed when they were taken.
        entries: list[dict[str, t.Any]] = []
        for cell in cells:
            for workload in workloads:
                entry = results.summarize_repeats(
                    functools.partial(
                        self.drain_cell,
                        cell,
                        workload,
                        backlog=options["tasks"],
                        timeout=options["timeout"],
                    ),
                    repeat=options["repeat"],
                    rank_by="tasks_per_sec",
                    metrics=("elapsed_s",),
                )
                self.report_cell(entry)
                entries.append(entry)

        self.report(entries)
        # Returned, not written: BaseCommand puts a truthy handle() result on stdout,
        # so the path lands there and in call_command's return value both.
        return str(results.write_run(RUN_NAME, {"entries": entries}))

    def drain_cell(
        self, cell: Cell, workload: str, *, backlog: int, timeout: float
    ) -> dict[str, t.Any]:
        truncate_queue_tables(DRAIN_QUEUE)
        ExecutionLog.objects.all().delete()
        enqueue_backlog(workload, backlog)

        with clocks.measure_phase(f"cell {cell.label} as {workload}") as phase:
            run_drain_workers(cell, timeout=timeout)
        elapsed_s = phase.elapsed_s

        executions = ExecutionLog.objects.count()
        distinct_tasks = ExecutionLog.objects.values("task_id").distinct().count()
        if distinct_tasks != backlog:
            msg = (
                f"Cell {cell.label} ran {distinct_tasks} of the {backlog} tasks it "
                f"enqueued on '{DRAIN_QUEUE}' as {workload}, so its workers exited "
                "with the backlog unfinished and there is no honest throughput to "
                "record for it. A task left retrying on a backoff outlives a draining "
                "worker, and so does one claimed by a worker that died."
            )
            raise CommandError(msg)

        return {
            "cell": cell.label,
            "workers": cell.workers,
            "concurrency": cell.concurrency,
            "workload": workload,
            "tasks": backlog,
            "elapsed_s": round(elapsed_s, 2),
            "tasks_per_sec": round(backlog / elapsed_s, 1),
            "executions": executions,
            "distinct_tasks": distinct_tasks,
            "duplicates": executions - distinct_tasks,
        }

    def report_cell(self, entry: dict[str, t.Any]) -> None:
        self.stdout.write(
            f"{entry['cell']} {entry['workload']}: {entry['tasks']} tasks in "
            f"{entry['elapsed_s']}s ({entry['tasks_per_sec']}/s, "
            f"{entry['duplicates']} duplicates) over {len(entry['runs'])} run(s), "
            f"spread {results.format_spread(entry, 'tasks_per_sec')}"
        )

    def report(self, entries: list[dict[str, t.Any]]) -> None:
        self.stdout.write(
            f"{'cell':<8}{'workload':<10}{'tasks':>8}"
            f"{'elapsed_s':>11}{'tasks_per_sec':>15}{'duplicates':>12}"
            f"{'runs':>6}{'spread':>21}"
        )
        for entry in entries:
            self.stdout.write(
                f"{entry['cell']:<8}{entry['workload']:<10}{entry['tasks']:>8}"
                f"{entry['elapsed_s']:>11}{entry['tasks_per_sec']:>15}"
                f"{entry['duplicates']:>12}{len(entry['runs']):>6}"
                f"{results.format_spread(entry, 'tasks_per_sec'):>21}"
            )


def parse_cell(spec: str) -> Cell:
    workers, _, concurrency = spec.partition("x")
    if not (workers.isdigit() and concurrency.isdigit()):
        msg = (
            f"Cannot read {spec!r} as a cell. Write it as workers by concurrency, "
            "both whole numbers — '4x4' is four workers at concurrency four."
        )
        raise CommandError(msg)
    if int(workers) < 1 or int(concurrency) < 1:
        msg = f"Cell {spec!r} measures nothing. Both halves must be at least 1."
        raise CommandError(msg)
    return Cell(spec, int(workers), int(concurrency))


def enqueue_backlog(workload: str, count: int) -> None:
    task_to_burn = tasks.burn_sync if workload == "sync" else tasks.burn_async
    routed = task_to_burn.using(queue_name=DRAIN_QUEUE)
    for index in range(count):
        routed.enqueue({"n": index})


def run_drain_workers(cell: Cell, *, timeout: float) -> None:
    environ = build_worker_environ()
    context = multiprocessing.get_context("spawn")
    children = [
        context.Process(
            target=manage.run_drain_worker,
            args=(DRAIN_QUEUE, cell.concurrency, environ),
        )
        for _ in range(cell.workers)
    ]
    for child in children:
        child.start()
    wait_for_workers(children, cell, timeout=timeout)


def build_worker_environ() -> dict[str, str]:
    """Spell the database this process is really using as the environment a child reads.

    Under pytest that is the test database, not ``loadtest.settings``'s default — a
    child that resolved the default would drain an empty queue and time nothing.
    """
    settings_dict = connections[resolve_absurd_database()].settings_dict
    return {
        "DJANGO_SETTINGS_MODULE": manage.SETTINGS_MODULE,
        "PGDATABASE": settings_dict["NAME"],
        "PGHOST": settings_dict["HOST"],
        "PGPORT": str(settings_dict["PORT"]),
        "PGUSER": settings_dict["USER"],
        "PGPASSWORD": settings_dict["PASSWORD"],
    }


def wait_for_workers(
    children: "t.Sequence[multiprocessing.process.BaseProcess]",
    cell: Cell,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    failures = []
    for index, child in enumerate(children):
        child.join(max(deadline - time.monotonic(), 0.0))
        if child.is_alive():
            child.kill()
            child.join()
            failures.append(f"worker {index} was still running after {timeout}s")
        elif child.exitcode != 0:
            failures.append(f"worker {index} exited {child.exitcode}")
    if failures:
        msg = (
            f"Cell {cell.label} has no timing to record: {', '.join(failures)}. "
            "A worker that raised printed its traceback above; one that was killed "
            "outlasted --timeout."
        )
        raise CommandError(msg)
