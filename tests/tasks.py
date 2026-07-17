import typing as t

from absurd_sdk import CancellationPolicy, RetryStrategy
from django.contrib.auth.models import Group
from django.tasks import TaskContext, task

from django_absurd.params import absurd_default_params
from tests.models import Payload


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def make_group(name: str) -> str:
    Group.objects.create(name=name)
    return name


@task
def boom() -> t.Never:
    msg = "boom"
    raise ValueError(msg)


@task(takes_context=True)
def report_attempt(context: TaskContext[t.Any, t.Any]) -> int:
    return context.attempt


@task(takes_context=True)
def report_args(
    context: TaskContext[t.Any, t.Any], *args: t.Any, **kwargs: t.Any
) -> list[t.Any]:
    return context.task_result.args


@task(queue_name="other")
def routed() -> str:
    Group.objects.create(name="routed")
    return "routed"


@task
@absurd_default_params(max_attempts=7)
def with_default_attempts(a: int, b: int) -> int:
    return a + b


@task
def echo(value: t.Any) -> t.Any:
    return value


@task
def create_payload(data: t.Any) -> int:
    return Payload.objects.create(data=data).pk


@task
@absurd_default_params(max_attempts=3)
def capped(a: int, b: int) -> int:
    return a + b


@task(queue_name="reports")
def on_reports() -> str:
    return "on_reports"


@task
@absurd_default_params(retry_strategy=RetryStrategy(kind="exponential", base_seconds=2))
def retrying() -> t.Never:
    msg = "path-resolved for its decorator; never run"
    raise NotImplementedError(msg)


@task
@absurd_default_params(cancellation=CancellationPolicy(max_duration=30))
def cancellable() -> t.Never:
    msg = "path-resolved for its decorator; never run"
    raise NotImplementedError(msg)


@task(queue_name="reports")
@absurd_default_params(
    max_attempts=9,
    retry_strategy=RetryStrategy(kind="fixed", base_seconds=5),
    cancellation=CancellationPolicy(max_duration=45, max_delay=3),
)
def fully_specced() -> t.Never:
    msg = "path-resolved for its decorator; never run"
    raise NotImplementedError(msg)
