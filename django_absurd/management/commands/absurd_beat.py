import signal
import threading
import typing as t
from types import FrameType

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_absurd.management.base import BEAT_DISABLED_UNDER_PG_CRON, resolve_backend
from django_absurd.scheduler import get_settings_schedules, run_beat


class Command(BaseCommand):
    help = "Start the Absurd beat scheduler."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--alias",
            default=None,
            help="Absurd backend alias (required when multiple Absurd backends exist).",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        _, backend = resolve_backend(options)

        if backend.scheduler == "pg_cron":
            raise CommandError(BEAT_DISABLED_UNDER_PG_CRON)

        stop = threading.Event()

        def handle_signal(signum: int, frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        schedules = get_settings_schedules(backend)
        cleanup = backend.options.get("CLEANUP")
        message = f"Started beat with {len(schedules)} schedule(s)."
        if cleanup:
            message += f" + cleanup: {cleanup['schedule']}"
        self.stdout.write(message)
        run_beat(backend, stop=stop)
