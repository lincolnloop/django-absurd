import contextlib
import datetime as dt
import json
import pathlib
import re
import threading
import time
import typing as t

import time_machine
from absurd_sdk import RetryStrategy
from django.tasks import task

from django_absurd import absurd_params

NAP_SECONDS = 30.0
NAP_INTERVAL_S = 0.02


def read_stage(results_dir: pathlib.Path, stage: str) -> dict[str, t.Any]:
    """Parse the results file a stage wrote, so a test can assert what is in it."""
    parsed: dict[str, t.Any] = json.loads(
        (results_dir / f"stage_{stage}.json").read_text()
    )
    return parsed


@contextlib.contextmanager
def nap_the_wall_clock() -> t.Iterator[None]:
    """Run the body on a host whose WALL clock keeps outrunning its monotonic one.

    That disagreement is the only input the harness's
    `host.check_phase_uninterrupted`
    reads, and a test cannot suspend its own machine. time-machine moves `time.time`
    and leaves `perf_counter` alone, which is exactly the shape of a nap; the thread
    repeats the jump so a phase is suspended whenever inside the body it starts.

    Deliberately NOT the `dj_absurd` fixture, which is the wrong tool twice over: it
    moves Postgres too, erasing the very disagreement under test, and it writes a GUC
    on the parent's connection that the `absurd_worker` children never see. Nothing in
    the harness's own process derives a database deadline from Python's wall clock —
    every drain deadline is `time.monotonic` and every recorded timestamp is a Postgres
    column — so moving that clock reaches the guard and nothing else.
    """
    stopping = threading.Event()
    with time_machine.travel(dt.datetime.now(tz=dt.UTC), tick=True) as traveller:
        napping = threading.Thread(
            target=nap_until_stopped, args=(traveller, stopping), daemon=True
        )
        napping.start()
        try:
            yield
        finally:
            stopping.set()
            napping.join()


def nap_until_stopped(
    traveller: time_machine.Traveller, stopping: threading.Event
) -> None:
    while not stopping.wait(NAP_INTERVAL_S):
        traveller.shift(dt.timedelta(seconds=NAP_SECONDS))


def normalize_measured_numbers(output: str) -> str:
    """Console output with every measured value blanked, so it reads as a literal.

    Only a number carrying a unit is blanked: a measurement's own name has a decimal
    in it too (`poll_0.05`), and erasing that would erase what the line is about.
    """
    return re.sub(r"\d+\.\d+(?=[ %]|ms)", "N", output)


def normalize_measured_durations(rep: dict[str, t.Any]) -> dict[str, t.Any]:
    """A refused rep with the durations in its error blanked, so it reads as a literal.

    Both numbers in the message are real elapsed times, which no test can pin; what a
    test can pin is that the guard named them.
    """
    return {**rep, "error": re.sub(r"\d+\.\d+s", "Ns", rep["error"])}


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
