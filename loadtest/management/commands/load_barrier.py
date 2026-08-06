"""Ask what a batch barrier costs when a backlog's tasks are not all the same length.

The SDK's async worker loop claims a batch, launches every task in it, and then waits
for the whole batch before it claims again::

    tasks = await self.claim_tasks(batch_size=effective_batch_size, ...)
    ...
    for task in tasks:
        promise = asyncio.create_task(self._execute_task(task, claim_timeout))
        ...
    await asyncio.gather(*executing)     # <-- the barrier

``batch_size`` defaults to ``concurrency``, so a worker of C slots claims C tasks and
cannot claim again until the slowest of the C has finished. The SDK's *sync* loop keeps
a rolling window instead and has no such barrier; django-absurd drives the async client,
so it inherits this one. Nothing here patches that — the harness ships no production
code, and this probe exists to put a number on the behaviour, not to change it.

A uniform backlog hides the barrier completely: when every task takes the same time,
waiting for the slowest of C costs nothing. So each arm is run twice over:

- ``uniform`` — every task the same length, the control;
- ``mixed`` — mostly ``--fast-seconds`` tasks with ``--slow`` slow ones spread evenly
  through the backlog.

Both carry the same task count *and the same total service time* — the uniform length is
the mixed backlog's mean — so the two differ in variance and in nothing else. Each is
drained twice at equal total slots: ``pooled`` is one worker at ``--concurrency`` C,
``split`` is C workers at concurrency 1. Separate processes each run their own loop, so
a straggler in ``split`` stalls only its own worker.

Wall clock alone cannot tell "the barrier stalled claims" from "slow tasks are slow", so
the deliverable is not a ratio. Each execution records the interval its slot was
occupied (``loadtest.models.OccupancyLog``) and the arm's timeline is reconstructed from
those intervals: ``mean_busy`` is how many slots were really executing, and
``idle_slot_s`` integrates idle slots *against a backlog that still had work in it* —
slot-seconds that were available, wanted, and not used. That number is the finding. An
arm that never drains its backlog fails the run instead of recording one.
"""

import bisect
import itertools
import multiprocessing
import multiprocessing.process
import time
import typing as t

from django.core.management.base import BaseCommand, CommandError

from django_absurd.flush import truncate_queue_tables
from loadtest import manage, results, tasks
from loadtest.management.commands.load_drain import build_worker_environ
from loadtest.management.commands.load_sleepers import stop_resident_worker
from loadtest.models import OccupancyLog

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser

RUN_NAME = "barrier"
SEEDED_QUEUE = "bulk"
DEFAULT_QUEUE = "alpha"
DEFAULT_FAST = 400
DEFAULT_SLOW = 20
DEFAULT_FAST_SECONDS = 0.01
DEFAULT_SLOW_SECONDS = 1.0
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_SECONDS = 600.0
WORKLOADS = ("sync", "async")
DISTRIBUTIONS = ("uniform", "mixed")
ARMS = ("pooled", "split")
POLL_INTERVAL_SECONDS = 0.05


class Probe(t.NamedTuple):
    queue: str
    fast: int
    slow: int
    fast_seconds: float
    slow_seconds: float
    concurrency: int
    batch_size: int | None
    timeout: float


class Topology(t.NamedTuple):
    arm: str
    label: str
    workers: int
    concurrency: int


class Interval(t.NamedTuple):
    started_s: float
    finished_s: float
    pid: int


class Occupancy(t.NamedTuple):
    span_s: float
    mean_busy: float
    max_busy: int
    utilization: float
    idle_slot_s: float
    idle_share: float
    pids: int
    ramp_s: float


class Command(BaseCommand):
    help = (
        "Measure what the worker's batch barrier costs on a backlog of mixed task "
        "durations, by draining the same backlog at equal total slots as one wide "
        "worker and as several narrow ones, against a same-total-work uniform "
        "control. Destructive: every arm empties the probed queue and the occupancy "
        "log before it enqueues."
    )

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--queue",
            default=DEFAULT_QUEUE,
            help=f"Queue to run the probe on (default: '{DEFAULT_QUEUE}').",
        )
        parser.add_argument(
            "--fast",
            type=int,
            default=DEFAULT_FAST,
            help=f"Short tasks in the backlog (default: {DEFAULT_FAST}).",
        )
        parser.add_argument(
            "--slow",
            type=int,
            default=DEFAULT_SLOW,
            help=(
                f"Long tasks in the backlog (default: {DEFAULT_SLOW}), spread evenly "
                "through it so a batch rarely holds two. Bunched at one end they "
                "would form batches of their own and stall nothing."
            ),
        )
        parser.add_argument(
            "--fast-seconds",
            type=float,
            default=DEFAULT_FAST_SECONDS,
            help=(
                "Seconds a short task holds its slot "
                f"(default: {DEFAULT_FAST_SECONDS})."
            ),
        )
        parser.add_argument(
            "--slow-seconds",
            type=float,
            default=DEFAULT_SLOW_SECONDS,
            help=(
                "Seconds a long task holds its slot "
                f"(default: {DEFAULT_SLOW_SECONDS}). It is the ratio to "
                "--fast-seconds that makes a straggler, not the absolute value."
            ),
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=DEFAULT_CONCURRENCY,
            help=(
                f"Total slots every arm gets (default: {DEFAULT_CONCURRENCY}) — one "
                "worker at this concurrency in the pooled arm, this many workers at "
                "concurrency 1 in the split one."
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help=(
                "Tasks the worker claims per poll cycle (default: unset, which leaves "
                "the SDK to claim exactly --concurrency of them). Raising it above "
                "--concurrency is what gives the loop's rolling window something to "
                "roll over: the barrier is then paid once per batch instead of once "
                "per generation of slots."
            ),
        )
        parser.add_argument(
            "--workload",
            choices=(*WORKLOADS, "both"),
            default="both",
            help="Task flavour of the backlog (default: both).",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_TIMEOUT_SECONDS,
            help=(
                "Seconds an arm gets to drain its backlog before the run is failed "
                f"(default: {DEFAULT_TIMEOUT_SECONDS}). An arm that outlasts it has "
                "no honest timing to record, however interesting the reason."
            ),
        )

    def handle(self, *args: t.Any, **options: t.Any) -> str:
        probe = build_probe(options)
        workloads = (
            WORKLOADS if options["workload"] == "both" else (options["workload"],)
        )

        # Reported arm by arm as well as in the closing table: a run that dies on a
        # later arm has still measured the earlier ones, and those numbers only survive
        # if they were printed when they were taken.
        entries: list[dict[str, t.Any]] = []
        for workload in workloads:
            for distribution in DISTRIBUTIONS:
                for arm in ARMS:
                    entry = self.measure_arm(probe, workload, distribution, arm)
                    self.report_arm(entry)
                    entries.append(entry)

        comparisons = [
            compare_topologies(entries, workload, distribution)
            for workload in workloads
            for distribution in DISTRIBUTIONS
        ]
        self.report(entries, comparisons)
        # Returned, not written: BaseCommand puts a truthy handle() result on stdout,
        # so the path lands there and in call_command's return value both.
        return str(
            results.write_run(
                RUN_NAME, {"entries": entries, "comparisons": comparisons}
            )
        )

    def measure_arm(
        self, probe: Probe, workload: str, distribution: str, arm: str
    ) -> dict[str, t.Any]:
        """Drain one backlog under one topology and reconstruct its slot timeline.

        The backlog is enqueued before a worker exists, so no arm's drain is rate-
        limited by this process's enqueue loop, and ``span_s`` is read back off the
        recorded intervals rather than off a clock here — which leaves every worker's
        interpreter start-up outside the measurement instead of inside it.
        """
        topology = plan_topology(arm, probe.concurrency)
        durations = plan_backlog(probe, distribution)
        truncate_queue_tables(probe.queue)
        OccupancyLog.objects.all().delete()
        enqueue_backlog(probe, workload, durations)

        started = time.perf_counter()
        workers = start_resident_workers(probe, topology)
        try:
            wait_for_backlog(workers, probe, len(durations))
        finally:
            for worker in workers:
                stop_resident_worker(worker)
        elapsed_s = time.perf_counter() - started

        occupancy = measure_occupancy(read_intervals(), probe.concurrency)
        return {
            "workload": workload,
            "distribution": distribution,
            "arm": topology.arm,
            "topology": topology.label,
            "workers": topology.workers,
            "concurrency": topology.concurrency,
            "batch_size": probe.batch_size,
            "slots": probe.concurrency,
            "tasks": len(durations),
            "fast": probe.fast,
            "slow": probe.slow,
            "fast_seconds": probe.fast_seconds,
            "slow_seconds": probe.slow_seconds,
            "work_s": round(sum(durations), 6),
            "elapsed_s": round(elapsed_s, 2),
            "span_s": round(occupancy.span_s, 2),
            "tasks_per_sec": round(len(durations) / occupancy.span_s, 1),
            "executions": OccupancyLog.objects.count(),
            "distinct_tasks": OccupancyLog.objects.values("task_id").distinct().count(),
            "duplicates": OccupancyLog.objects.count()
            - OccupancyLog.objects.values("task_id").distinct().count(),
            "pids": occupancy.pids,
            "ramp_s": round(occupancy.ramp_s, 2),
            "mean_busy": round(occupancy.mean_busy, 2),
            "max_busy": occupancy.max_busy,
            "utilization": round(occupancy.utilization, 3),
            "idle_slot_s": round(occupancy.idle_slot_s, 2),
            "idle_share": round(occupancy.idle_share, 3),
        }

    def report_arm(self, entry: dict[str, t.Any]) -> None:
        self.stdout.write(
            f"{entry['workload']} {entry['distribution']} {entry['arm']} "
            f"({entry['topology']}): {entry['tasks']} tasks in {entry['span_s']}s "
            f"({entry['tasks_per_sec']}/s), {entry['mean_busy']} of "
            f"{entry['slots']} slots busy, {entry['idle_slot_s']}s of idle slot time "
            f"with the backlog still full"
        )

    def report(
        self, entries: list[dict[str, t.Any]], comparisons: list[dict[str, t.Any]]
    ) -> None:
        self.stdout.write(
            f"\n{'workload':<8}{'spread':<9}{'topology':<10}{'batch':>7}{'span_s':>9}"
            f"{'tasks_per_sec':>15}{'mean_busy':>11}{'utilization':>13}"
            f"{'idle_slot_s':>13}{'idle_share':>12}"
        )
        for entry in entries:
            self.stdout.write(
                f"{entry['workload']:<8}{entry['distribution']:<9}"
                f"{entry['topology']:<10}{name_batch_size(entry['batch_size']):>7}"
                f"{entry['span_s']:>9}"
                f"{entry['tasks_per_sec']:>15}{entry['mean_busy']:>11}"
                f"{entry['utilization']:>13}{entry['idle_slot_s']:>13}"
                f"{entry['idle_share']:>12}"
            )
        self.stdout.write(
            f"\n{'workload':<8}{'spread':<9}{'pooled_s':>10}{'split_s':>9}"
            f"{'ratio':>7}{'pooled_idle_slot_s':>20}{'split_idle_slot_s':>19}"
        )
        for comparison in comparisons:
            self.stdout.write(
                f"{comparison['workload']:<8}{comparison['distribution']:<9}"
                f"{comparison['pooled_s']:>10}{comparison['split_s']:>9}"
                f"{comparison['ratio']:>7}{comparison['pooled_idle_slot_s']:>20}"
                f"{comparison['split_idle_slot_s']:>19}"
            )


def build_probe(options: dict[str, t.Any]) -> Probe:
    if options["queue"] == SEEDED_QUEUE:
        msg = (
            f"Refusing to probe '{SEEDED_QUEUE}': every arm truncates the queue it "
            f"runs on, and '{SEEDED_QUEUE}' is where load_seed puts the seeded "
            "dataset the admin probe reads. Pick another declared queue."
        )
        raise CommandError(msg)
    for name in ("fast", "slow", "concurrency"):
        if options[name] < 1:
            msg = f"--{name} must be at least 1."
            raise CommandError(msg)
    for name in ("fast_seconds", "slow_seconds"):
        if options[name] <= 0:
            msg = f"--{name.replace('_', '-')} must be greater than 0."
            raise CommandError(msg)
    if options["batch_size"] is not None and options["batch_size"] < 1:
        msg = (
            "--batch-size must be at least 1, or left unset to claim exactly "
            "--concurrency tasks per cycle."
        )
        raise CommandError(msg)
    return Probe(
        queue=options["queue"],
        fast=options["fast"],
        slow=options["slow"],
        fast_seconds=options["fast_seconds"],
        slow_seconds=options["slow_seconds"],
        concurrency=options["concurrency"],
        batch_size=options["batch_size"],
        timeout=options["timeout"],
    )


def name_batch_size(batch_size: int | None) -> str:
    """Label an unset ``--batch-size`` in the table rather than leaving the cell blank.

    A blank cell reads as "not measured"; the arm was measured, the worker just picked
    the batch size itself.
    """
    return "auto" if batch_size is None else str(batch_size)


def plan_topology(arm: str, concurrency: int) -> Topology:
    if arm == "pooled":
        return Topology(arm, f"1x{concurrency}", 1, concurrency)
    return Topology(arm, f"{concurrency}x1", concurrency, 1)


def plan_backlog(probe: Probe, distribution: str) -> list[float]:
    """Lay out the durations of one arm's backlog, in the order they are enqueued.

    The uniform control is the mixed backlog's mean, so both distributions carry the
    same task count and the same total service time — leaving variance as the only
    difference between them, which is the whole point of having a control.

    Floor division places the long tasks: ``index * total // slow`` is strictly
    increasing while ``total >= slow``, which it always is, so the backlog holds
    exactly ``--slow`` of them and the two distributions' totals really do match.
    """
    total = probe.fast + probe.slow
    work_s = probe.fast * probe.fast_seconds + probe.slow * probe.slow_seconds
    if distribution == "uniform":
        return [work_s / total] * total
    slow_at = {index * total // probe.slow for index in range(probe.slow)}
    return [
        probe.slow_seconds if index in slow_at else probe.fast_seconds
        for index in range(total)
    ]


def enqueue_backlog(probe: Probe, workload: str, durations: list[float]) -> None:
    toil = tasks.toil_sync if workload == "sync" else tasks.toil_async
    routed = toil.using(queue_name=probe.queue)
    for seconds in durations:
        routed.enqueue({"seconds": seconds})


def start_resident_workers(
    probe: Probe, topology: Topology
) -> "list[multiprocessing.process.BaseProcess]":
    """Start an arm's workers, spawned rather than forked.

    A forked child inherits this process's open Postgres connections, and two processes
    writing one socket is not a measurement, it is a corruption. Resident rather than
    burst: a burst worker runs django-absurd's own drain loop, and the barrier under
    test is the SDK's blocking one.
    """
    environ = build_worker_environ()
    context = multiprocessing.get_context("spawn")
    workers: list[multiprocessing.process.BaseProcess] = [
        context.Process(
            target=manage.run_resident_worker,
            args=(probe.queue, topology.concurrency, environ, probe.batch_size),
        )
        for _ in range(topology.workers)
    ]
    for worker in workers:
        worker.start()
    return workers


def wait_for_backlog(
    workers: "t.Sequence[multiprocessing.process.BaseProcess]",
    probe: Probe,
    target: int,
) -> None:
    """Block until the whole backlog has executed, or fail the arm.

    Polled coarsely on purpose: this process shares the database with the workers whose
    slot occupancy it is measuring, and the timeline is read off the workers' own
    stamps afterwards, so noticing late costs the measurement nothing.
    """
    deadline = time.monotonic() + probe.timeout
    while True:
        check_workers_alive(workers)
        if count_executed_tasks() >= target:
            return
        if time.monotonic() > deadline:
            msg = (
                f"The arm executed {count_executed_tasks()} of {target} tasks within "
                f"{probe.timeout}s, so it has no timeline to record. Raise --timeout "
                "if the backlog is simply larger than the budget."
            )
            raise CommandError(msg)
        time.sleep(POLL_INTERVAL_SECONDS)


def count_executed_tasks() -> int:
    return OccupancyLog.objects.values("task_id").distinct().count()


def check_workers_alive(
    workers: "t.Sequence[multiprocessing.process.BaseProcess]",
) -> None:
    for index, worker in enumerate(workers):
        if worker.is_alive():
            continue
        msg = (
            f"Worker {index} exited {worker.exitcode} while the arm was still waiting "
            "on it, so the rest of the backlog can never arrive. A worker that raised "
            "printed its traceback above."
        )
        raise CommandError(msg)


def read_intervals() -> list[Interval]:
    return [
        Interval(row.started_at.timestamp(), row.finished_at.timestamp(), row.pid)
        for row in OccupancyLog.objects.all()
    ]


def measure_occupancy(intervals: list[Interval], slots: int) -> Occupancy:
    """Rebuild how many slots were executing at each moment, and what that wasted.

    ``idle_slot_s`` is the deliverable: idle slot-seconds charged only while the
    backlog still held work, which is ``min(slots - busy, queued)`` integrated over the
    span. Capping by ``queued`` is what keeps the number honest — the tail of any drain
    has idle slots because there is nothing left to run, and that is not a stall.

    Every task is enqueued before a worker starts, so a task is queued exactly until it
    starts; ``bisect`` over the sorted stamps answers both counts at any instant.
    """
    origin = min(interval.started_s for interval in intervals)
    starts = sorted(interval.started_s - origin for interval in intervals)
    finishes = sorted(interval.finished_s - origin for interval in intervals)
    span_s = finishes[-1]
    if span_s <= 0:
        msg = (
            "Every execution of this arm started and finished in the same instant, so "
            "there is no timeline to measure. Raise --fast-seconds above the clock's "
            "resolution."
        )
        raise CommandError(msg)

    busy_slot_s = sum(
        interval.finished_s - interval.started_s for interval in intervals
    )
    idle_slot_s = 0.0
    max_busy = 0
    for point, next_point in itertools.pairwise(sorted({*starts, *finishes})):
        started = bisect.bisect_right(starts, point)
        busy = started - bisect.bisect_right(finishes, point)
        queued = len(starts) - started
        max_busy = max(max_busy, busy)
        idle_slot_s += min(max(slots - busy, 0), queued) * (next_point - point)

    slot_s = slots * span_s
    return Occupancy(
        span_s=span_s,
        mean_busy=busy_slot_s / span_s,
        max_busy=max_busy,
        utilization=busy_slot_s / slot_s,
        idle_slot_s=idle_slot_s,
        idle_share=idle_slot_s / slot_s,
        pids=len({interval.pid for interval in intervals}),
        ramp_s=measure_ramp(intervals),
    )


def measure_ramp(intervals: list[Interval]) -> float:
    """How long after the first execution the last worker joined in.

    Reported rather than corrected for: workers are started together but each pays its
    own interpreter start-up, so an arm of several workers spends its opening moments
    short-handed and some of ``idle_slot_s`` is that rather than a barrier. This bounds
    how much, and it bounds it against the split arm — the one the barrier hypothesis
    expects to win.
    """
    first_by_pid: dict[int, float] = {}
    for interval in intervals:
        first_by_pid[interval.pid] = min(
            first_by_pid.get(interval.pid, interval.started_s), interval.started_s
        )
    return max(first_by_pid.values()) - min(first_by_pid.values())


def compare_topologies(
    entries: list[dict[str, t.Any]], workload: str, distribution: str
) -> dict[str, t.Any]:
    arms = {
        entry["arm"]: entry
        for entry in entries
        if entry["workload"] == workload and entry["distribution"] == distribution
    }
    return {
        "workload": workload,
        "distribution": distribution,
        "pooled_s": arms["pooled"]["span_s"],
        "split_s": arms["split"]["span_s"],
        "ratio": round(arms["pooled"]["span_s"] / arms["split"]["span_s"], 2),
        "pooled_idle_slot_s": arms["pooled"]["idle_slot_s"],
        "split_idle_slot_s": arms["split"]["idle_slot_s"],
    }
