import signal
import threading
import typing as t

from django.core.management.base import BaseCommand

from django_absurd.management.base import resolve_backend
from django_absurd.scheduler import get_settings_schedules, run_beat


class Command(BaseCommand):
    help = "Start the Absurd beat scheduler."

    def add_arguments(self, parser: t.Any) -> None:
        parser.add_argument(
            "--alias",
            default=None,
            help="Absurd backend alias (required when multiple Absurd backends exist).",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        _, backend = resolve_backend(options)

        stop = threading.Event()

        def handle_signal(signum: int, frame: t.Any) -> None:
            stop.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        schedules = get_settings_schedules(backend)
        self.stdout.write(f"Started beat with {len(schedules)} schedule(s).")
        run_beat(backend, stop=stop)
