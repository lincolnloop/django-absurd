import time
import typing as t

import absurd_sdk
from absurd_sdk import CancellationPolicy, JsonValue, RetryStrategy
from django.contrib.auth.models import Group
from django.tasks import TaskContext, task

from django_absurd import absurd_params, get_absurd_context
from tests.models import Payload

SYNC_STEP_CALLS: dict[str, int] = {"n": 0}
SYNC_CHARGE_ATTEMPTS: dict[str, int] = {"n": 0}
RETRY_CALLS: dict[str, int] = {"n": 0}
WEEK_SECONDS = 7 * 24 * 3600


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
def report_attempt(context: "TaskContext[t.Any, t.Any]") -> int:
    return context.attempt


@task(takes_context=True)
def report_args(
    context: "TaskContext[t.Any, t.Any]", *args: t.Any, **kwargs: t.Any
) -> list[t.Any]:
    return context.task_result.args


@task(takes_context=True)
def report_context(context: "TaskContext[t.Any, t.Any]") -> dict[str, JsonValue]:
    return {
        "id": context.task_result.id,
        "backend": context.task_result.backend,
        "attempts": context.task_result.attempts,
    }


@task(queue_name="other")
def routed() -> str:
    Group.objects.create(name="routed")
    return "routed"


@task
@absurd_params(max_attempts=7)
def with_default_attempts(a: int, b: int) -> t.Never:
    msg = "path-resolved for its decorator; never run"
    raise NotImplementedError(msg)


@task
def echo(value: t.Any) -> t.Any:
    return value


@task
def create_payload(data: JsonValue) -> int:
    return Payload.objects.create(data=data).pk


@task
@absurd_params(max_attempts=3)
def capped(a: int, b: int) -> int:
    return a + b


@task(queue_name="reports")
def on_reports() -> str:
    return "on_reports"


@task
@absurd_params(retry_strategy=RetryStrategy(kind="exponential", base_seconds=2))
def retrying() -> t.Never:
    msg = "boom"
    raise ValueError(msg)


@task
@absurd_params(cancellation=CancellationPolicy(max_duration=30))
def cancellable() -> t.Never:
    msg = "path-resolved for its decorator; never run"
    raise NotImplementedError(msg)


@task(queue_name="reports")
@absurd_params(
    max_attempts=9,
    retry_strategy=RetryStrategy(kind="fixed", base_seconds=5),
    cancellation=CancellationPolicy(max_duration=45, max_delay=3),
)
def fully_specced() -> t.Never:
    msg = "path-resolved for its decorator; never run"
    raise NotImplementedError(msg)


@task
def sstep_echo(value: str) -> str:
    return get_absurd_context().step("echo", lambda: value)


@task
def scoverage() -> dict[str, JsonValue]:
    context = get_absurd_context()
    context.heartbeat()
    tenant = context.headers.get("tenant")

    @context.run_step
    def bare() -> str:
        return "bare-val"

    @context.run_step()
    def derived() -> str:
        return "derived-val"

    @context.run_step("custom")
    def named() -> str:
        return "named-val"

    return {"tenant": tenant, "bare": bare, "derived": derived, "named": named}


@task
def ssleep_for_once(key: str) -> int:
    context = get_absurd_context()

    def bump() -> int:
        SYNC_STEP_CALLS["n"] += 1
        return SYNC_STEP_CALLS["n"]

    n = context.step("bump", bump)
    context.sleep_for("nap", WEEK_SECONDS)
    return n


@task
def ssleep_until_once(key: str) -> str:
    get_absurd_context().sleep_until("nap", time.time() + WEEK_SECONDS)
    return "woke"


@task
def sawait_event_once(name: str) -> t.Any:
    return get_absurd_context().await_event(name)


@task
def semit_event_once(name: str, payload: t.Any) -> None:
    get_absurd_context().emit_event(name, payload)


@task
def sawait_event_timeout(name: str, timeout: int) -> str:
    try:
        get_absurd_context().await_event(name, timeout=timeout)
    except absurd_sdk.TimeoutError:
        return "timed-out"
    return "no-timeout"


@task
def await_the_probe_event() -> t.Any:
    return get_absurd_context().await_event("probe.go", timeout=3600)


@task
def emit_the_probe_event() -> None:
    get_absurd_context().emit_event("probe.go", {"go": True})


@task
def spawn_child_then_return(value: int) -> str:
    run_child.enqueue(value)
    return "spawned"


@task
def run_child(value: int) -> int:
    return value * 2


@task
def scharge_then_fail_once() -> str:
    context = get_absurd_context()

    def charge() -> str:
        return "charged"

    result = context.step("charge", charge)
    SYNC_CHARGE_ATTEMPTS["n"] += 1
    if SYNC_CHARGE_ATTEMPTS["n"] == 1:
        msg = "scharge_then_fail_once"
        raise ValueError(msg)
    return result


@task
def sleep_a_week() -> str:
    get_absurd_context().sleep_for("nap", WEEK_SECONDS)
    return "woke"


@task
def sleep_twice() -> str:
    context = get_absurd_context()
    context.sleep_for("first", WEEK_SECONDS)
    context.sleep_for("second", 3 * 24 * 3600)
    return "woke-twice"


@task
@absurd_params(retry_strategy=RetryStrategy(kind="fixed", base_seconds=3600))
def fail_with_long_backoff() -> t.Never:
    msg = "boom"
    raise ValueError(msg)


@task
@absurd_params(cancellation=CancellationPolicy(max_delay=60))
def cancellable_after_a_minute() -> t.Never:
    msg = "cancelled before it ever ran"
    raise NotImplementedError(msg)


@task
@absurd_params(retry_strategy=RetryStrategy(kind="fixed", base_seconds=0))
def fail_twice_then_succeed() -> str:
    RETRY_CALLS["n"] += 1
    if RETRY_CALLS["n"] < 3:
        msg = f"attempt {RETRY_CALLS['n']} fails"
        raise ValueError(msg)
    return "third-time-lucky"
