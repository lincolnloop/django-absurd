import signal
import threading
import typing as t
from types import FrameType

from django.core.management.base import CommandError

from django_absurd import console
from django_absurd import logging as absurd_logging
from django_absurd.management.base import (
    BEAT_DISABLED_UNDER_PG_CRON,
    AbsurdCommand,
    resolve_backend,
)
from django_absurd.scheduler import get_settings_schedules, run_beat


class Command(AbsurdCommand):
    help = "Start the Absurd beat scheduler."

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        absurd_logging.attach_console_handler()
        backend = resolve_backend()

        if backend.scheduler == "pg_cron":
            raise CommandError(BEAT_DISABLED_UNDER_PG_CRON)

        stop = threading.Event()

        def handle_signal(signum: int, frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        schedules = get_settings_schedules(backend)
        cleanup = backend.options.get("CLEANUP")
        drum = console.build_glyph_prefix(self.stdout, "🥁")
        message = f"{drum}Started beat with {len(schedules)} schedule(s)."
        if cleanup:
            message += f" + cleanup: {cleanup['schedule']}"
        self.stdout.write(message)
        run_beat(backend, stop=stop)
