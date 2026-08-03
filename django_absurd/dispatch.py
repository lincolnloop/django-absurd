"""The one place every django-absurd signal send goes through.

Routing sends through here means a receiver's exception can never reach the task: the
send is wrapped in its own try/except, logged, and swallowed.
"""

import logging
import typing as t

from django.dispatch import Signal
from django.tasks.signals import task_enqueued, task_finished, task_started

if t.TYPE_CHECKING:
    from django.tasks import TaskResult

logger = logging.getLogger("django_absurd")

# A Signal's repr carries nothing identifying, and a caught exception's traceback holds
# no frame above the raise, so neither says WHICH send a receiver broke.
SIGNAL_NAMES: dict[Signal, str] = {
    task_enqueued: "task_enqueued",
    task_finished: "task_finished",
    task_started: "task_started",
}


def send_task_signal(
    signal: Signal, sender: type, task_result: "TaskResult[t.Any, t.Any]"
) -> None:
    """Send ``signal`` for ``task_result``, logging (not raising) a receiver failure.

    Catches ``Exception``, never ``BaseException``: ``asyncio.CancelledError`` at
    shutdown must still propagate.
    """
    try:
        signal.send(sender, task_result=task_result)
    except Exception:
        # A missing name degrades to the generic word: a KeyError raised here would
        # escape the containment and reach the task.
        logger.exception(
            "django-absurd %s receiver failed for task result id=%s",
            SIGNAL_NAMES.get(signal, "signal"),
            task_result.id,
        )
