import pytest
from django.core.management import CommandError

from loadtest import clocks


def test_a_phase_whose_clocks_agree_is_accepted() -> None:
    clocks.check_phase_uninterrupted(elapsed_s=10.0, wall_s=10.05, label="an arm")


def test_a_phase_the_machine_slept_through_is_refused() -> None:
    with pytest.raises(CommandError) as caught:
        clocks.check_phase_uninterrupted(elapsed_s=11.1, wall_s=209.5, label="an arm")

    assert "suspended" in str(caught.value)


def test_the_refusal_names_the_arm_and_both_clocks() -> None:
    with pytest.raises(CommandError) as caught:
        clocks.check_phase_uninterrupted(
            elapsed_s=11.1, wall_s=209.5, label="sync mixed"
        )

    message = str(caught.value)
    assert "sync mixed" in message
    assert "11.1" in message
    assert "209.5" in message


def test_a_wall_clock_running_behind_the_monotonic_one_is_accepted() -> None:
    """Only lost monotonic time means a suspension; the other direction is a clock step.

    A backwards NTP correction shortens the wall interval, which cannot manufacture
    execution time the way a suspension does, so it is not this guard's business.
    """
    clocks.check_phase_uninterrupted(elapsed_s=10.0, wall_s=4.0, label="an arm")


def test_measuring_a_phase_reports_the_monotonic_elapsed_time() -> None:
    with clocks.measure_phase("an arm") as phase:
        pass

    assert phase.elapsed_s >= 0
    assert phase.wall_s >= 0
