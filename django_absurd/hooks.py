"""Hooks passed to both Absurd clients, where Absurd's own lifecycle becomes visible.

Every hook body here is contained: the SDK runs hooks inside the same try/except that
wraps a task's own handler, so an exception escaping one of ours is indistinguishable
from the task itself failing — it consumes an attempt and lands in ``failure_reason``,
with nothing reaching stderr. Catch, log, and continue on every path.
"""

import logging

from absurd_sdk import AbsurdHooks, JsonValue, SpawnOptions

logger = logging.getLogger(__name__)


def build_absurd_hooks() -> AbsurdHooks:
    """Build the hooks dict passed to both the sync and async Absurd clients."""
    return {"before_spawn": log_before_spawn}


def log_before_spawn(
    task_name: str, params: JsonValue, options: SpawnOptions
) -> SpawnOptions:
    """Log a spawn, then return ``options`` unchanged.

    The SDK assigns this function's return value straight back into its own
    spawn_options (``spawn_options = before_spawn(task_name, params, spawn_options)``),
    so ``options`` must come back exactly as given on every path, including the one
    where logging itself raises — a hook that returns ``None`` breaks every spawn in
    the project.
    """
    try:
        logger.debug("spawn requested: %s", describe_spawn(task_name, options))
    except Exception:
        logger.exception("failed to log spawn: name=%s", task_name)
    return options


def describe_spawn(task_name: str, options: SpawnOptions) -> str:
    # The Absurd-side detail Django's own enqueue line omits: queue, retry ceiling,
    # and dedup key. max_attempts/idempotency_key are absent from options entirely
    # when the caller didn't set them, so they're reported only when present.
    detail = f"name={task_name} queue={options.get('queue')}"
    if "max_attempts" in options:
        detail += f" max_attempts={options['max_attempts']}"
    if "idempotency_key" in options:
        detail += f" idempotency_key={options['idempotency_key']}"
    return detail
