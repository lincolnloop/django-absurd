import typing as t

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser

from django_absurd.management.base import (
    BEAT_DISABLED_UNDER_PG_CRON,
    AbsurdReportCommand,
    resolve_backend,
)
from django_absurd.queues import provision_backend
from django_absurd.worker import WorkerOptions, run_burst_worker, run_worker


class Command(AbsurdReportCommand):
    help = "Start the Absurd task worker."

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--queue",
            default="default",
            help="Queue name to consume (default: 'default').",
        )
        parser.add_argument(
            "--burst",
            action="store_true",
            help="Drain the queue then exit (no persistent blocking loop).",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=1,
            help=(
                "Max tasks run concurrently: async tasks on the event loop, "
                "sync tasks in a thread pool of this size (default: 1)."
            ),
        )
        parser.add_argument(
            "--claim-timeout",
            type=int,
            default=120,
            help="Seconds before a claimed task is returned to queue (default: 120).",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=0.25,
            help="Seconds between polling the queue for tasks (default: 0.25).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Maximum tasks to claim per poll cycle (default: None).",
        )
        parser.add_argument(
            "--worker-id",
            default=None,
            help="Worker identifier; SDK synthesizes <host>:<pid> when omitted.",
        )
        parser.add_argument(
            "--beat",
            action="store_true",
            help=(
                "Run the beat scheduler in the worker loop"
                " (not compatible with --burst)."
            ),
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        backend = resolve_backend()
        queue = options["queue"]

        if options["burst"] and options["beat"]:
            msg = "--beat is not compatible with --burst."
            raise CommandError(msg)

        if options["beat"] and backend.scheduler == "pg_cron":
            raise CommandError(BEAT_DISABLED_UNDER_PG_CRON)

        worker_options = WorkerOptions(
            concurrency=options["concurrency"],
            claim_timeout=options["claim_timeout"],
            poll_interval=options["poll_interval"],
            batch_size=options["batch_size"],
            worker_id=options["worker_id"],
        )

        if options["burst"]:
            result = run_burst_worker(queue, options=worker_options)
            self.report_sync_result(result)
            self.stdout.write(f"Started worker on queue '{queue}'.")
            return

        if queue not in backend.queues:
            valid = ", ".join(sorted(backend.queues))
            msg = (
                f"Queue '{queue}' is not declared for backend '{backend.alias}'."
                f" Valid queues: {valid}"
            )
            raise CommandError(msg)

        try:
            # Full provision on start so the admin views reflect every declared
            # queue, not just the one this worker serves.
            result = provision_backend(backend)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc
        self.report_sync_result(result)

        self.stdout.write(f"Started worker on queue '{queue}'.")
        run_worker(
            backend,
            queue,
            run_beat=options["beat"],
            options=worker_options,
        )
