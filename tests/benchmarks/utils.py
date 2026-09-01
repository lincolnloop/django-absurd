import json
import pathlib
import time
import typing as t

from absurd_sdk import RetryStrategy
from django.tasks import task

from django_absurd import absurd_params


def read_stage(results_dir: pathlib.Path, stage: str) -> dict[str, t.Any]:
    """Parse the results file a stage wrote, so a test can assert what is in it."""
    parsed: dict[str, t.Any] = json.loads(
        (results_dir / f"stage_{stage}.json").read_text()
    )
    return parsed


@task(queue_name="bench")
@absurd_params(
    max_attempts=2, retry_strategy=RetryStrategy(kind="fixed", base_seconds=0)
)
def sleep_past_claim_lease(seconds: float = 2.0) -> int:
    """Outlives its claim lease so Absurd's expired-lease sweep redelivers it.

    Capped at two attempts: with the default five this would loop for half a minute.
    """
    time.sleep(seconds)
    return 0


@task(queue_name="bench")
@absurd_params(max_attempts=1)
def fail_on_its_only_attempt() -> t.Never:
    """Terminally fails without redelivery, so the task ends 'failed' with no run of
    its own ever completing — the shape that shrinks a measurement's sample silently."""
    msg = "benchmark fixture: this task always fails"
    raise RuntimeError(msg)
