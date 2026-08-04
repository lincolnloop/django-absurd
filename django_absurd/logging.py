"""A default destination for django-absurd's own log lines.

The worker and beat commands run in the foreground and their whole job is to report
what Absurd is doing, so they should not be silent out of the box. Django's default
configuration covers the ``django`` logger only, and the root logger's default level is
WARNING, so without this a fresh project sees no task lines at all.

Deliberately NOT clever: the only question asked is whether the project's own
``LOGGING`` declares this package. If it does, that configuration is the whole story
and nothing here runs. Inspecting the live logger hierarchy instead — ancestors,
descendants, effective levels — is re-implementing what ``LOGGING`` already expresses.
"""

import logging
import typing as t

from django.conf import settings

LOGGER_NAME = "django_absurd"


def attach_console_handler() -> None:
    """Make this package's INFO lines visible, unless the project speaks for itself."""
    config = getattr(settings, "LOGGING", None)
    if declares_absurd_logger(config):
        return
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers or logger.level != logging.NOTSET:
        return
    # A root handler already catches our records, since propagate stays True. Adding
    # ours underneath would print every line twice — once bare, once through theirs,
    # which for a JSON root handler also puts an unstructured line in a structured
    # stream. Raise the level anyway: root's own level may be filtering us out before
    # its handler ever sees a record.
    if not declares_root_handlers(config):
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)


def declares_absurd_logger(config: t.Any) -> bool:
    """Whether a ``LOGGING`` dict names ``django_absurd`` or one of its children.

    A child counts: a project that configured ``django_absurd.worker`` has said what it
    wants from the worker, and a handler on the parent would duplicate those lines.
    """
    if not isinstance(config, dict):
        return False
    loggers = config.get("loggers")
    if not isinstance(loggers, dict):
        return False
    return any(
        name == LOGGER_NAME or name.startswith(f"{LOGGER_NAME}.") for name in loggers
    )


def declares_root_handlers(config: t.Any) -> bool:
    """Whether a ``LOGGING`` dict puts handlers on the root logger.

    Root catches our records by propagation, so this is the difference between one
    copy of each line and two. Both spellings count: ``dictConfig`` accepts a
    top-level ``root`` key and a ``""`` entry under ``loggers``.
    """
    if not isinstance(config, dict):
        return False
    loggers = config.get("loggers")
    candidates = [config.get("root")]
    if isinstance(loggers, dict):
        candidates.append(loggers.get(""))
    return any(
        isinstance(entry, dict) and entry.get("handlers") for entry in candidates
    )
