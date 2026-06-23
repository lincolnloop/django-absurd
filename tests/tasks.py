from django.contrib.auth.models import Group
from django.tasks import task

from django_absurd.params import absurd_default_params


@task
def add(a, b):
    return a + b


# Module-level (undecorated) coroutine: applying @task to it must be rejected
# because the backend sets supports_async_task=False. Kept undecorated so the
# module imports cleanly; the test applies task() to it to trigger validation.
async def add_async(a, b):
    return a + b


@task
def make_group(name):
    Group.objects.create(name=name)
    return name


@task
def boom():
    msg = "boom"
    raise ValueError(msg)


@task(takes_context=True)
def report_attempt(context):
    return context.attempt


@task(takes_context=True)
def report_args(context, *args, **kwargs):
    return context.task_result.args


@task(queue_name="other")
def routed():
    Group.objects.create(name="routed")
    return "routed"


@task
@absurd_default_params(max_attempts=7)
def with_default_attempts(a, b):
    return a + b


@task
def echo(value):
    return value
