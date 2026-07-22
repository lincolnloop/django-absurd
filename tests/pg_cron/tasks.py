"""Minimal, dependency-free task used only by test_sync_schedules_on_migrate.py's
subprocess-based real-DB test — must not import anything beyond django.tasks (no
django.contrib.auth, no tests.models) so a bare, minimally-configured subprocess can
resolve its dotted path. Never actually invoked (only referenced by dotted path as a
SCHEDULE target), so its body is a deliberate no-op."""

from django.tasks import task


@task
def add(a: int, b: int) -> int:
    msg = "tests.pg_cron.tasks.add is a dotted-path placeholder, never invoked"
    raise NotImplementedError(msg)
