# Imported for the decoration: ``@task`` validates ``queue_name`` against the live
# ``settings.TASKS``, so these must load before a test narrows QUEUES. Per-suite rather
# than parent — tests/multidb replaces TASKS with a backend declaring no QUEUES at all.
from tests import atasks, tasks  # noqa: F401
