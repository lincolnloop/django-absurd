"""Make the package's own lifecycle logging visible without a project ``LOGGING`` dict.

Django's ``DEFAULT_LOGGING`` configures the ``django`` logger only; ``django_absurd`` is
untouched and the root logger is unconfigured, so ``logging.lastResort`` applies and
only WARNING and above ever reach stderr. ``attach_console_handler`` is what the worker
and beat commands call so their INFO lines are visible out of the box.
"""

import logging


def attach_console_handler() -> None:
    """Make ``django_absurd``'s INFO lines visible, without a project ``LOGGING`` dict.

    Two independent decisions, both idempotent:

    - **Handler:** attach one plain ``StreamHandler`` only when nothing on the
      logger's ancestor chain (``hasHandlers()``) or its descendant loggers
      (``descendant_has_a_propagating_handler``) would already catch these
      records. Records propagate both directions along that chain: a project
      that configures only the root logger would otherwise get every line
      twice once this attaches underneath it, and a project that configures a
      child logger (e.g. ``django_absurd.hooks``) would otherwise get every one
      of that child's lines twice once this attaches above it. No ``Formatter``
      of ours; the default one is fine and a formatter is the thing this
      feature deliberately does not ship.
    - **Level:** raise the logger to INFO only when it has no explicit level of
      its own (``NOTSET``) *and* its current effective level is coarser than
      INFO. Both conditions matter: the ``NOTSET`` check alone is the
      documented opt-out (a project that sets ``django_absurd`` to WARNING in
      its own ``LOGGING`` keeps us quiet), and the effective-level check stops
      this from overriding an *inherited* level that is already at or finer
      than INFO — a project whose root is at DEBUG must keep seeing DEBUG
      lines from this package, not have them raised away to INFO.
    """
    logger = logging.getLogger("django_absurd")
    if not logger.hasHandlers() and not descendant_has_a_propagating_handler(logger):
        logger.addHandler(logging.StreamHandler())
    if logger.level == logging.NOTSET and logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)


def descendant_has_a_propagating_handler(logger: logging.Logger) -> bool:
    """Report whether some logger under ``logger`` would double-print if we attach.

    ``hasHandlers()`` only walks ``logger`` and its ancestors; a project that
    configured a *child* instead (e.g. ``django_absurd.hooks``) is invisible to
    it. ``Logger.manager.loggerDict`` holds every logger name the process has
    ever requested — including ``logging.PlaceHolder`` entries for ancestor
    names nobody called ``getLogger`` on directly, which carry no ``.handlers``
    and must be skipped rather than inspected. A descendant with
    ``propagate = False`` is excluded too: its own records never reach a
    handler placed on ``logger`` regardless of what we do here, so its handler
    poses no duplication risk and must not block us from covering everything
    else under ``logger``.
    """
    prefix = f"{logger.name}."
    for name, candidate in logging.Logger.manager.loggerDict.items():
        if not name.startswith(prefix) or isinstance(candidate, logging.PlaceHolder):
            continue
        if candidate.handlers and candidate.propagate:
            return True
    return False
