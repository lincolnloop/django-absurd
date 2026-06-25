import typing as t

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends
from django_absurd.management.base import AbsurdReportCommand
from django_absurd.queues import reconcile_queue
from django_absurd.worker import WorkerOptions, run_worker


class Command(AbsurdReportCommand):
    help = "Start the Absurd task worker."

    def add_arguments(self, parser: t.Any) -> None:
        parser.add_argument(
            "--queue",
            default="default",
            help="Queue name to consume (default: 'default').",
        )
        parser.add_argument(
            "--alias",
            default=None,
            help="Absurd backend alias (required when multiple Absurd backends exist).",
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

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        backends = get_absurd_backends()
        alias = options["alias"]
        queue = options["queue"]

        if alias is not None:
            if alias not in backends:
                valid = ", ".join(sorted(backends))
                msg = (
                    f"'{alias}' is not an Absurd backend alias. Valid aliases: {valid}"
                )
                raise CommandError(msg)
            backend = backends[alias]
        elif len(backends) == 1:
            alias, backend = next(iter(backends.items()))
        else:
            aliases = ", ".join(sorted(backends))
            msg = (
                f"Multiple Absurd backends found: {aliases}. Use --alias to select one."
            )
            raise CommandError(msg)

        if queue not in backend.queues:
            valid = ", ".join(sorted(backend.queues))
            msg = (
                f"Queue '{queue}' is not declared for backend '{alias}'."
                f" Valid queues: {valid}"
            )
            raise CommandError(msg)

        try:
            result = reconcile_queue(backend, queue)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc
        self.report_sync_result(result)

        worker_options = WorkerOptions(
            concurrency=options["concurrency"],
            claim_timeout=options["claim_timeout"],
            poll_interval=options["poll_interval"],
            batch_size=options["batch_size"],
            worker_id=options["worker_id"],
        )
        self.stdout.write(f"Started worker on queue '{queue}'.")
        run_worker(backend, queue, burst=options["burst"], options=worker_options)
