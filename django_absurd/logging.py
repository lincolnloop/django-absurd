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

    - **Handler:** attach one plain ``StreamHandler`` only when ``hasHandlers()`` is
      false — checked with ``hasHandlers()`` rather than this logger's own
      ``.handlers``, because records propagate: a project that configures only the
      root logger (or any other ancestor) would otherwise get every line twice once
      this attaches underneath it. No ``Formatter`` of ours; the default one is fine
      and a formatter is the thing this feature deliberately does not ship.
    - **Level:** set the logger to INFO only when it has no explicit level of its own
      (``NOTSET``), whether or not a handler got attached above. A project that
      configures a handler somewhere on the ancestor chain but leaves it at the
      default WARNING would otherwise have every INFO line filtered at the effective
      -level check before it ever reaches that handler — silent despite having "a
      handler". ``NOTSET`` is also the documented opt-out: a project that sets
      ``django_absurd`` to WARNING in its own ``LOGGING`` keeps us quiet.
    """
    logger = logging.getLogger("django_absurd")
    if not logger.hasHandlers():
        logger.addHandler(logging.StreamHandler())
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
