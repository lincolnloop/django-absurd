import typing as t

from django.core.management.base import CommandError

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser

from django_absurd import console
from django_absurd import logging as absurd_logging
from django_absurd.exceptions import QueueNotDeclaredError
from django_absurd.management.base import (
    BEAT_DISABLED_UNDER_PG_CRON,
    AbsurdCommand,
    resolve_backend,
)
from django_absurd.worker import WorkerOptions, run_worker


class Command(AbsurdCommand):
    help = "Start the Absurd task worker."

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--queue",
            default="default",
            help="Queue name to consume (default: 'default').",
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
            help="Run the beat scheduler in the worker loop.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        absurd_logging.attach_console_handler()
        backend = resolve_backend()
        queue = options["queue"]

        if options["beat"] and backend.scheduler == "pg_cron":
            raise CommandError(BEAT_DISABLED_UNDER_PG_CRON)

        worker_options = WorkerOptions(
            concurrency=options["concurrency"],
            claim_timeout=options["claim_timeout"],
            poll_interval=options["poll_interval"],
            batch_size=options["batch_size"],
            worker_id=options["worker_id"],
        )

        if queue not in backend.queues:
            raise QueueNotDeclaredError(queue, backend.alias, backend.queues)

        elephant = console.build_glyph_prefix(self.stdout, "🐘")

        def announce_started() -> None:
            self.stdout.write(f"{elephant}Started worker on queue '{queue}'.")

        def announce_stop_requested() -> None:
            self.stdout.write(
                f"{elephant}Stop requested on queue '{queue}'; "
                "finishing in-flight tasks."
            )

        run_worker(
            backend,
            queue,
            run_beat=options["beat"],
            options=worker_options,
            on_started=announce_started,
            on_stop_requested=announce_stop_requested,
        )
        self.stdout.write(f"{elephant}Stopped worker on queue '{queue}'.")
