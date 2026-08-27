"""Two clocks per measured phase, because one of them stops when the machine sleeps.

``time.perf_counter()`` is ``mach_absolute_time`` on macOS and does not advance while
the system is suspended. A laptop that sleeps mid-arm therefore reports a short and
entirely plausible elapsed time for a phase that really took minutes — while the
tasks caught in flight record wall-clock intervals spanning the whole nap, and any
claim that outlived its timeout is redelivered. Nothing in the numbers looks wrong;
the arm is simply fiction.

So every phase is bracketed by both clocks and refused when they disagree. Detection
rather than prevention: ``caffeinate`` can be worth holding over a run, but an assertion
that quietly lapses leaves no trace, and a guard that fails loudly does.
"""

import contextlib
import time
import typing as t

from django.core.management import CommandError

# Generous next to a suspension (seconds to minutes) and far above the sub-millisecond
# gap between reading two clocks in sequence.
SUSPENSION_TOLERANCE_SECONDS = 2.0


class Phase:
    """The monotonic and wall-clock cost of one measured phase."""

    def __init__(self) -> None:
        self.elapsed_s = 0.0
        self.wall_s = 0.0


@contextlib.contextmanager
def measure_phase(label: str) -> "t.Iterator[Phase]":
    """Time a phase on both clocks, refusing it if the machine slept through it."""
    phase = Phase()
    started = time.perf_counter()
    started_wall = time.time()
    yield phase
    phase.elapsed_s = time.perf_counter() - started
    phase.wall_s = time.time() - started_wall
    check_phase_uninterrupted(phase.elapsed_s, phase.wall_s, label)


def check_phase_uninterrupted(elapsed_s: float, wall_s: float, label: str) -> None:
    """Refuse a phase whose wall clock outran its monotonic one.

    Only lost monotonic time is a suspension. The opposite direction is a clock step —
    it cannot manufacture execution time the way a nap does, so it is not refused here.
    """
    lost_s = wall_s - elapsed_s
    if lost_s <= SUSPENSION_TOLERANCE_SECONDS:
        return
    msg = (
        f"The machine was suspended for about {lost_s:.1f}s while measuring {label}: "
        f"the monotonic clock counted {elapsed_s:.1f}s and the wall clock "
        f"{wall_s:.1f}s. "
        "time.perf_counter() does not advance during a suspension, so this arm's "
        "timings are fiction — tasks caught in flight recorded intervals spanning the "
        "nap, and any claim that outlived its timeout was redelivered. Re-run it on a "
        "machine that will stay awake (`caffeinate -is` holds it) rather than trusting "
        "the number."
    )
    raise CommandError(msg)
