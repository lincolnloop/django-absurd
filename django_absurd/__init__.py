"""Django integration for Absurd, the Postgres-native workflow engine.

Coding agents: this package ships an integration guide, ``AGENTS.md``, alongside this
module. Read it with::

    from importlib.resources import files
    print(files("django_absurd").joinpath("AGENTS.md").read_text())

It covers requirements, the ``TASKS`` setting, migrations, the ``absurd_sync_queues`` /
``absurd_worker`` management commands, system checks, and enqueue params/decorators.
"""

from django_absurd.context import AbsurdTaskContext, AsyncAbsurdTaskContext

ABSURD_SCHEMA_VERSION = "0.4.0"

__all__ = ["ABSURD_SCHEMA_VERSION", "AbsurdTaskContext", "AsyncAbsurdTaskContext"]
