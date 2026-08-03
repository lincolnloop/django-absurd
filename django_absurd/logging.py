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
    """Attach one plain ``StreamHandler`` at INFO, unless the project speaks for
    itself.
    """
    if declares_absurd_logger(getattr(settings, "LOGGING", None)):
        return
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return
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
