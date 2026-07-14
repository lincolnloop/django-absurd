from absurd_sdk import CancellationPolicy, RetryStrategy
from django.contrib.auth.models import Group
from django.tasks import task

from django_absurd.params import absurd_default_params
from tests.models import Payload


@task
def add(a, b):
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


@task
def create_payload(data):
    return Payload.objects.create(data=data).pk


@task
@absurd_default_params(max_attempts=3)
def capped(a, b):
    return a + b


@task(queue_name="reports")
def on_reports():
    return "on_reports"


@task
@absurd_default_params(retry_strategy=RetryStrategy(kind="exponential", base_seconds=2))
def retrying():
    return "retrying"


@task
@absurd_default_params(cancellation=CancellationPolicy(max_duration=30))
def cancellable():
    return "cancellable"
