"""Make the package's own lifecycle logging visible without a project ``LOGGING`` dict.

Django's ``DEFAULT_LOGGING`` configures the ``django`` logger only; ``django_absurd`` is
untouched and the root logger is unconfigured, so ``logging.lastResort`` applies and
only WARNING and above ever reach stderr. ``attach_console_handler`` is what the worker
and beat commands call so their INFO lines are visible out of the box.
"""

import logging


def attach_console_handler() -> None:
    """Attach one plain ``StreamHandler`` at INFO to the ``django_absurd`` logger.

    Idempotent, and a no-op when the project has already configured something that
    would handle these records — checked with ``hasHandlers()`` rather than this
    logger's own ``.handlers``, because records propagate: a project that configures
    only the root logger (or any other ancestor) would otherwise get every line twice
    once this attaches underneath it. No ``Formatter`` of ours; the default one is
    fine and a formatter is the thing this feature deliberately does not ship.
    """
    logger = logging.getLogger("django_absurd")
    if logger.hasHandlers():
        return
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
