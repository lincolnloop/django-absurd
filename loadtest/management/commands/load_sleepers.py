"""Ask whether a durable sleep costs a worker slot, and answer it with two timings.

``context.sleep_for`` on a SYNC task hops onto the worker's event loop through
``SyncAbsurdContext.run_on_loop`` while the task body itself sits in a thread of a pool
sized to ``--concurrency``. If that thread stayed parked for the sleep's duration, N
sleeping tasks would hold N of the worker's C slots and everything enqueued behind them
would starve. The docs say the task suspends durably and the worker resumes it later —
that is the claim under test, not a premise, and an async sleeper is measured beside the
sync one because only the sync path crosses the thread pool.

Each workload gets two arms, run one at a time so they never measure each other:

- ``control`` — one worker, no sleepers, drain ``--quick`` ordinary tasks;
- ``sleepers`` — the same worker and the same ``--quick`` tasks, with ``--sleepers``
  tasks already suspended in a sleep far longer than the measurement window.

``elapsed_s`` covers enqueueing the quick batch and waiting for the last of it to
execute; ``enqueue_s`` is reported beside it so the client-side share is visible. Both
arms pay the same worker start-up and the same warm-up task before the clock starts, so
their ratio is the finding: near 1 means a sleep released its slot, far above 1 means it
did not, and an arm that never drains fails the run instead of recording a number.

The timings are corroborated by something that does not depend on a clock at all: while
the quick batch drains, the sleepers' own runs are counted by state. A sleeper in
``running`` holds a claim and therefore a slot; a sleeper in ``sleeping`` holds neither.
``running_max`` is the worst moment observed, ``sleeping_min`` the least-asleep one.
"""

import functools
import multiprocessing
import multiprocessing.process
import time
import typing as t

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

import django_absurd.models
from django_absurd.backends import decode_result_id
from django_absurd.flush import truncate_queue_tables
from loadtest import clocks, manage, preflight, results, tasks
from loadtest.management.commands.load_drain import build_worker_environ
from loadtest.models import ExecutionLog

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser

RUN_NAME = "sleepers"
SEEDED_QUEUE = "bulk"
DEFAULT_QUEUE = "alpha"
DEFAULT_SLEEPERS = 100
DEFAULT_QUICK = 200
DEFAULT_CONCURRENCY = 4
DEFAULT_SLEEP_SECONDS = 3600.0
DEFAULT_TIMEOUT_SECONDS = 300.0
WORKLOADS = ("sync", "async")
POLL_INTERVAL_SECONDS = 0.02
SAMPLE_INTERVAL_SECONDS = 0.25
STOP_GRACE_SECONDS = 30.0


class Probe(t.NamedTuple):
    queue: str
    sleepers: int
    quick: int
    concurrency: int
    sleep_seconds: float
    timeout: float


class StateSample(t.NamedTuple):
    sleeping: int
    running: int


class Drain(t.NamedTuple):
    elapsed_s: float
    enqueue_s: float
    executions: int
    distinct_tasks: int
    samples: list[StateSample]


class Command(BaseCommand):
    help = (
        "Measure whether tasks suspended in a durable sleep hold a worker's "
        "concurrency slots, by draining the same batch of quick tasks with and "
        "without a crowd of sleepers present. Destructive: every arm empties the "
        "probed queue and the execution log before it enqueues."
    )

    def add_arguments(self, parser: "CommandParser") -> None:
        results.add_repeat_argument(parser)
        parser.add_argument(
            "--queue",
            default=DEFAULT_QUEUE,
            help=f"Queue to run the probe on (default: '{DEFAULT_QUEUE}').",
        )
        parser.add_argument(
            "--sleepers",
            type=int,
            default=DEFAULT_SLEEPERS,
            help=(
                "Tasks parked in a durable sleep before the quick batch is enqueued "
                f"(default: {DEFAULT_SLEEPERS}). Keep it well above --concurrency: "
                "fewer sleepers than the worker has slots cannot starve anything."
            ),
        )
        parser.add_argument(
            "--quick",
            type=int,
            default=DEFAULT_QUICK,
            help=(
                f"Ordinary tasks drained and timed per arm (default: {DEFAULT_QUICK})."
            ),
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=DEFAULT_CONCURRENCY,
            help=f"Concurrency of the one worker (default: {DEFAULT_CONCURRENCY}).",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=DEFAULT_SLEEP_SECONDS,
            help=(
                "How long each sleeper sleeps for "
                f"(default: {DEFAULT_SLEEP_SECONDS}). It only has to outlast the "
                "measurement window: a sleeper that wakes mid-arm stops being a "
                "sleeper and starts being a quick task nobody counted."
            ),
        )
        parser.add_argument(
            "--workload",
            choices=(*WORKLOADS, "both"),
            default="both",
            help="Task flavour of the sleepers and the quick batch (default: both).",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_TIMEOUT_SECONDS,
            help=(
                "Seconds any one phase of an arm gets before the run is failed "
                f"(default: {DEFAULT_TIMEOUT_SECONDS}). A quick batch that outlasts "
                "it is the starvation this probe is looking for, and it is reported "
                "as a failure rather than as a timing."
            ),
        )

    def handle(self, *args: t.Any, **options: t.Any) -> str:
        preflight.require_migrated_database()
        probe = build_probe(options)
        workloads = (
            WORKLOADS if options["workload"] == "both" else (options["workload"],)
        )

        # Reported arm by arm as well as in the closing table: a run that dies on a
        # later arm has still measured the earlier ones, and those numbers only survive
        # if they were printed when they were taken.
        entries: list[dict[str, t.Any]] = []
        for workload in workloads:
            for sleeper_count in (0, probe.sleepers):
                entry = results.summarize_repeats(
                    functools.partial(self.measure_arm, probe, workload, sleeper_count),
                    repeat=options["repeat"],
                    rank_by="elapsed_s",
                    metrics=("tasks_per_sec",),
                )
                self.report_arm(entry)
                entries.append(entry)

        comparisons = [compare_arms(entries, workload) for workload in workloads]
        self.report(entries, comparisons)
        # Returned, not written: BaseCommand puts a truthy handle() result on stdout,
        # so the path lands there and in call_command's return value both.
        return str(
            results.write_run(
                RUN_NAME, {"entries": entries, "comparisons": comparisons}
            )
        )

    def measure_arm(
        self, probe: Probe, workload: str, sleeper_count: int
    ) -> dict[str, t.Any]:
        truncate_queue_tables(probe.queue)
        ExecutionLog.objects.all().delete()
        sleeper_ids = enqueue_sleepers(probe, workload, sleeper_count)

        worker = start_resident_worker(probe)
        try:
            wait_for_sleeping_runs(worker, probe, sleeper_ids)
            # Both arms pay one quick task before the clock starts, so neither arm's
            # timing carries the worker's first-claim costs — the pool thread, the
            # task import, the child's own Django connection.
            warm_up_worker(worker, probe, workload)
            ExecutionLog.objects.all().delete()
            drain = drain_quick_tasks(worker, probe, workload, sleeper_ids)
        finally:
            stop_resident_worker(worker)

        return {
            "workload": workload,
            "arm": "sleepers" if sleeper_count else "control",
            "sleepers": sleeper_count,
            "quick": probe.quick,
            "concurrency": probe.concurrency,
            "sleep_seconds": probe.sleep_seconds,
            "enqueue_s": round(drain.enqueue_s, 2),
            "elapsed_s": round(drain.elapsed_s, 2),
            "tasks_per_sec": round(probe.quick / drain.elapsed_s, 1),
            "executions": drain.executions,
            "distinct_tasks": drain.distinct_tasks,
            "duplicates": drain.executions - drain.distinct_tasks,
            "state_samples": len(drain.samples),
            "sleeping_min": min(s.sleeping for s in drain.samples),
            "running_max": max(s.running for s in drain.samples),
        }

    def report_arm(self, entry: dict[str, t.Any]) -> None:
        self.stdout.write(
            f"{entry['workload']} {entry['arm']}: {entry['quick']} quick tasks in "
            f"{entry['elapsed_s']}s ({entry['tasks_per_sec']}/s) behind "
            f"{entry['sleepers']} sleepers, of which at worst "
            f"{entry['running_max']} were running and at best "
            f"{entry['sleeping_min']} were sleeping"
        )

    def report(
        self, entries: list[dict[str, t.Any]], comparisons: list[dict[str, t.Any]]
    ) -> None:
        self.stdout.write(
            f"{'workload':<10}{'arm':<10}{'sleepers':>9}{'quick':>7}"
            f"{'elapsed_s':>11}{'tasks_per_sec':>15}"
            f"{'sleeping_min':>14}{'running_max':>13}{'runs':>6}{'spread':>20}"
        )
        for entry in entries:
            self.stdout.write(
                f"{entry['workload']:<10}{entry['arm']:<10}{entry['sleepers']:>9}"
                f"{entry['quick']:>7}{entry['elapsed_s']:>11}"
                f"{entry['tasks_per_sec']:>15}{entry['sleeping_min']:>14}"
                f"{entry['running_max']:>13}{len(entry['runs']):>6}"
                f"{results.format_spread(entry, 'elapsed_s'):>20}"
            )
        self.stdout.write(
            f"\n{'workload':<10}{'control_s':>11}{'sleepers_s':>12}{'ratio':>8}"
        )
        for comparison in comparisons:
            self.stdout.write(
                f"{comparison['workload']:<10}{comparison['control_s']:>11}"
                f"{comparison['sleepers_s']:>12}{comparison['ratio']:>8}"
            )


def build_probe(options: dict[str, t.Any]) -> Probe:
    if options["queue"] == SEEDED_QUEUE:
        msg = (
            f"Refusing to probe '{SEEDED_QUEUE}': every arm truncates the queue it "
            f"runs on, and '{SEEDED_QUEUE}' is where load_seed puts the seeded "
            "dataset the admin probe reads. Pick another declared queue."
        )
        raise CommandError(msg)
    for name in ("sleepers", "quick", "concurrency"):
        if options[name] < 1:
            msg = f"--{name} must be at least 1."
            raise CommandError(msg)
    return Probe(
        queue=options["queue"],
        sleepers=options["sleepers"],
        quick=options["quick"],
        concurrency=options["concurrency"],
        sleep_seconds=options["sleep_seconds"],
        timeout=options["timeout"],
    )


def enqueue_sleepers(probe: Probe, workload: str, count: int) -> list[str]:
    """Enqueue ``count`` sleepers and return the task ids their runs are keyed by.

    An enqueued result id is ``"<queue>:<task_id>"``; the runs view stores the bare
    ``task_id``, so decode with the backend's own decoder rather than splitting here.
    """
    task_to_nap = tasks.nap_sync if workload == "sync" else tasks.nap_async
    routed = task_to_nap.using(queue_name=probe.queue)
    result_ids = [
        routed.enqueue({"seconds": probe.sleep_seconds}).id for _ in range(count)
    ]
    return [decode_result_id(result_id)[1] for result_id in result_ids]


def start_resident_worker(probe: Probe) -> "multiprocessing.process.BaseProcess":
    """Start the one worker of an arm, spawned rather than forked.

    A forked child inherits this process's open Postgres connections, and two
    processes writing one socket is not a measurement, it is a corruption.
    """
    context = multiprocessing.get_context("spawn")
    worker = context.Process(
        target=manage.run_resident_worker,
        args=(probe.queue, probe.concurrency, build_worker_environ()),
    )
    worker.start()
    return worker


def wait_for_sleeping_runs(
    worker: "multiprocessing.process.BaseProcess",
    probe: Probe,
    sleeper_ids: list[str],
) -> None:
    """Block until every sleeper's run reads ``sleeping``, or fail the arm.

    Establishing them first is what makes the comparison mean anything: a quick batch
    racing sleepers that have not reached their sleep call yet would be measuring the
    sleepers' start-up, not their suspension.
    """
    if not sleeper_ids:
        return
    deadline = time.monotonic() + probe.timeout
    while True:
        check_worker_alive(worker)
        if count_sleeper_states(probe, sleeper_ids).sleeping == len(sleeper_ids):
            return
        if time.monotonic() > deadline:
            sample = count_sleeper_states(probe, sleeper_ids)
            msg = (
                f"Only {sample.sleeping} of {len(sleeper_ids)} sleepers reached the "
                f"'sleeping' state within {probe.timeout}s ({sample.running} were "
                "still running), so there was never the crowd of sleepers this arm "
                "claims to measure against."
            )
            raise CommandError(msg)
        time.sleep(POLL_INTERVAL_SECONDS)


def warm_up_worker(
    worker: "multiprocessing.process.BaseProcess", probe: Probe, workload: str
) -> None:
    enqueue_quick_tasks(probe, workload, 1)
    wait_for_executions(worker, probe, 1, phase="its warm-up task")


def drain_quick_tasks(
    worker: "multiprocessing.process.BaseProcess",
    probe: Probe,
    workload: str,
    sleeper_ids: list[str],
) -> Drain:
    """Time the quick batch from enqueue to last execution, sampling as it goes.

    The clock starts before the enqueue rather than after it: the worker claims the
    first task while the rest are still being written, so there is no moment after the
    enqueue that the drain has not already begun. Both arms enqueue the same number of
    tasks the same way, so the shared client-side cost cancels in the ratio.
    """
    with clocks.measure_phase(f"the quick batch as {workload}") as phase:
        started = time.perf_counter()
        enqueue_quick_tasks(probe, workload, probe.quick)
        enqueue_s = time.perf_counter() - started
        samples = wait_for_executions(
            worker, probe, probe.quick, sleeper_ids=sleeper_ids
        )
    elapsed_s = phase.elapsed_s
    return Drain(
        elapsed_s=elapsed_s,
        enqueue_s=enqueue_s,
        executions=ExecutionLog.objects.count(),
        distinct_tasks=ExecutionLog.objects.values("task_id").distinct().count(),
        samples=samples,
    )


def enqueue_quick_tasks(probe: Probe, workload: str, count: int) -> None:
    task_to_burn = tasks.burn_sync if workload == "sync" else tasks.burn_async
    routed = task_to_burn.using(queue_name=probe.queue)
    for index in range(count):
        routed.enqueue({"n": index})


def wait_for_executions(
    worker: "multiprocessing.process.BaseProcess",
    probe: Probe,
    target: int,
    *,
    sleeper_ids: list[str] | None = None,
    phase: str = "its quick batch",
) -> list[StateSample]:
    """Block until ``target`` distinct tasks have executed, sampling the sleepers.

    Sampling is rate-limited rather than run every poll: the state read is a query
    against the union view, and one per 20ms would have this process competing with
    the worker for the very database whose throughput it is timing. The first sample
    is taken before the first completion check, so an arm always records at least one
    even when its batch drains between two polls.
    """
    deadline = time.monotonic() + probe.timeout
    samples: list[StateSample] = []
    next_sample = 0.0
    while True:
        check_worker_alive(worker)
        if time.monotonic() >= next_sample:
            samples.append(count_sleeper_states(probe, sleeper_ids or []))
            next_sample = time.monotonic() + SAMPLE_INTERVAL_SECONDS
        if ExecutionLog.objects.values("task_id").distinct().count() >= target:
            return samples
        if time.monotonic() > deadline:
            done = ExecutionLog.objects.values("task_id").distinct().count()
            msg = (
                f"The worker executed {done} of {target} tasks of {phase} within "
                f"{probe.timeout}s, so this arm has no timing to record. With "
                "sleepers present that is the starvation this probe looks for, and "
                "it is a finding, not a number."
            )
            raise CommandError(msg)
        time.sleep(POLL_INTERVAL_SECONDS)


def count_sleeper_states(probe: Probe, sleeper_ids: list[str]) -> StateSample:
    """Count the sleepers' runs by state — the timing-free half of the answer.

    Read through ``django_absurd.models.Run``, the shipped union view, so the probe
    never spells a per-queue table name of its own. ``Run`` is exported as
    ``type[models.Model]``, which carries no manager for a type checker, so the local
    ``type[t.Any]`` is the same boundary ``django_absurd.worker`` draws around its own
    dynamic-model reads.
    """
    if not sleeper_ids:
        return StateSample(sleeping=0, running=0)
    run_model: type[t.Any] = django_absurd.models.Run
    counts = run_model.objects.filter(
        queue=probe.queue, task_id__in=sleeper_ids
    ).aggregate(
        sleeping=Count("pk", filter=Q(state="sleeping")),
        running=Count("pk", filter=Q(state="running")),
    )
    return StateSample(sleeping=counts["sleeping"], running=counts["running"])


def check_worker_alive(worker: "multiprocessing.process.BaseProcess") -> None:
    if worker.is_alive():
        return
    msg = (
        f"The arm's worker exited {worker.exitcode} while the probe was still "
        "waiting on it, so nothing it was waiting for can ever arrive. A worker that "
        "raised printed its traceback above."
    )
    raise CommandError(msg)


def stop_resident_worker(worker: "multiprocessing.process.BaseProcess") -> None:
    """Stop the arm's worker, leaving nothing of it behind for the next arm.

    ``terminate`` is ``SIGTERM``, which ``absurd_worker`` installs a handler for — the
    worker leaves its poll loop and closes its own connection. Killing is the fallback
    for one that ignored it, and neither is an error here: the worker is resident, so
    every arm ends by stopping one, including an arm that already failed.
    """
    if not worker.is_alive():
        return
    worker.terminate()
    worker.join(STOP_GRACE_SECONDS)
    if worker.is_alive():
        worker.kill()
        worker.join()


def compare_arms(entries: list[dict[str, t.Any]], workload: str) -> dict[str, t.Any]:
    arms = {e["arm"]: e for e in entries if e["workload"] == workload}
    control_s = arms["control"]["elapsed_s"]
    sleepers_s = arms["sleepers"]["elapsed_s"]
    return {
        "workload": workload,
        "control_s": control_s,
        "sleepers_s": sleepers_s,
        "ratio": round(sleepers_s / control_s, 2),
    }
