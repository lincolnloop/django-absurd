"""Demo tasks. The worker discovers @task functions declared in each app's tasks.py."""

from django.contrib.auth.models import User
from django.tasks import task


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def create_user(username: str) -> str:
    """A task with a DB side effect — runs through the worker's ORM connection.

    Uses get_or_create because Absurd delivers at-least-once: a task may run more
    than once (e.g. retry after a crash), so handlers should be idempotent.
    """
    User.objects.get_or_create(username=username)
    return username


@task
async def create_user_async(username: str) -> str:
    """An ``async def`` task — runs natively on the worker's event loop, using
    Django's async ORM. Same idempotent get_or_create, async variant.
    """
    await User.objects.aget_or_create(username=username)
    return username
