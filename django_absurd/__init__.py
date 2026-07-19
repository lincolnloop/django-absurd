"""Django integration for Absurd, the Postgres-native workflow engine.

Coding agents: this package ships an integration guide, ``AGENTS.md``, alongside this
module. Read it with::

    from importlib.resources import files
    print(files("django_absurd").joinpath("AGENTS.md").read_text())

It covers requirements, the ``TASKS`` setting, migrations, the ``absurd_sync_queues`` /
``absurd_worker`` management commands, system checks, enqueue params/decorators, and
durable steps & sleep (``get_absurd_context`` / ``aget_absurd_context``).
"""

from django_absurd.context import (
    AbsurdTaskContext,
    aget_absurd_context,
    get_absurd_context,
)

ABSURD_SCHEMA_VERSION = "0.4.0"

__all__ = [
    "ABSURD_SCHEMA_VERSION",
    "AbsurdTaskContext",
    "aget_absurd_context",
    "get_absurd_context",
]
