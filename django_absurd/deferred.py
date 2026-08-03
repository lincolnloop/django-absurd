"""Deferred enqueue: the wrapper's name, and the handler that runs it.

A deferred enqueue spawns a wrapper row rather than the caller's task, so the caller's
task is never claimed before its work exists. ``backends`` spawns that row and
``worker`` dispatches it, so the name they agree on lives here — importable by both
without a cycle.

There is deliberately no ``@task``: decorating one resolves ``task_backends["default"]``
and validates its queue at decoration time, so a project whose ``QUEUES`` omits
``"default"`` would raise ``InvalidTask`` on import — at dispatch, where it takes the
worker down. ``worker.LazyTaskRegistry`` builds the handler instead.
"""

import asyncio
import datetime as dt
import logging
import typing as t

from absurd_sdk import AsyncTaskContext, JsonValue
from django.db import close_old_connections
from django.utils.module_loading import import_string

from django_absurd import params as params_module

if t.TYPE_CHECKING:
    # backends imports DEFER_NAME_SUFFIX from here, so this one stays type-only.
    from django_absurd.backends import TaskParams

logger = logging.getLogger(__name__)

# A deferred enqueue spawns `<target dotted path>:run_after` rather than the caller's
# task. Leading with the target sorts the row beside it in the admin and names the kwarg
# that caused it.
#
# The colon is load-bearing: it cannot appear in a dotted path, so this can never
# collide with a real task name. A bare `.run_after` would — a user's own task called
# `run_after` would be read as a deferral of the module containing it.
DEFER_NAME_SUFFIX = ":run_after"


def build_deferred_handler(
    target: str,
) -> t.Callable[["TaskParams", AsyncTaskContext], t.Awaitable[JsonValue]]:
    """Handle one deferred run: sleep until due, then enqueue ``target``.

    The enqueue is a checkpointed step, so a retry of this run replays the stored id
    rather than enqueueing a second time. Enqueuing is synchronous Django work, so it
    goes to a thread — the same hop ``worker.build_handler`` makes for a sync task.
    """

    async def handler(params: "TaskParams", ctx: AsyncTaskContext) -> JsonValue:
        spec = params["kwargs"]
        logger.info(
            "deferred task waiting: target=%s due=%s task_id=%s",
            target,
            spec["due"],
            ctx.task_id,
        )
        await ctx.sleep_until(
            "wait-until-due", dt.datetime.fromisoformat(str(spec["due"]))
        )

        async def enqueue_the_target() -> JsonValue:
            return await asyncio.to_thread(
                enqueue_deferred_target,
                target,
                list(spec["args"]),
                dict(spec["kwargs"]),
                str(spec["queue"]),
                dict(spec["options"]),
            )

        # Named for what it enqueued: the Checkpoints changelist shows no task name,
        # and checkpoint_name is searchable, so the target belongs in it.
        return await ctx.step(f"enqueue:{target}", enqueue_the_target)

    return handler


def enqueue_deferred_target(
    target: str,
    args: list[t.Any],
    kwargs: dict[str, t.Any],
    queue: str,
    options: dict[str, t.Any],
) -> str:
    """Enqueue the caller's task, returning its ``TaskResult.id``."""
    close_old_connections()
    try:
        target_task = import_string(target)
        if target_task.queue_name != queue:
            target_task = target_task.using(queue_name=queue)
        if options:
            target_task = params_module.absurd_params(**options).bind(target_task)
        return str(target_task.enqueue(*args, **kwargs).id)
    finally:
        close_old_connections()
