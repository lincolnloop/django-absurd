# Plan: `benchmarks/` supersedes `loadtest/`

Spec:
[`docs/specs/2026-09-09-benchmarks-supersede-loadtest.md`](../specs/2026-09-09-benchmarks-supersede-loadtest.md).

Branch `bench__durable-checkpoints`. One commit per task. TDD: RED first, always.

The tasks below are as approved. Where one differs from what was built, the code is
right and [Departed from this plan](#departed-from-this-plan-and-why) says why.

Gate per task: `uv run pytest tests/benchmarks/<file> -q --no-cov` while iterating,
`uv run pre-commit run --all-files`, then `tox -e bench_harness` before the commit.
`benchmarks/` coverage is gated at 100%, so every branch needs a test or must not exist.

## Task 1 — idle slot-seconds from Absurd's own columns

Shared by task 2. No new table, no task-side bookkeeping.

**RED.** `tests/benchmarks/test_analysis.py`: hand-written run intervals with a known
slot count and a known backlog window; assert `idle_slot_s`. Cases that must
discriminate: every slot busy the whole window (0); one long task with C-1 slots free
while work remained (the free slot-seconds); slots idle only AFTER the backlog emptied
(still 0 — that qualifier is the metric).

**Prose.** SQL reading run intervals for a queue since a window start, plus a reducer
folding intervals + slot count into one figure. `mean_busy` and `span_s` stay internal.

## Task 2 — `batch_barrier`

**RED.** `test_cli.py`: drive `batch_barrier --reps 1` tiny; assert the two arm names,
that each spec records its distribution, that both distributions carry equal total
service time (assert it, do not trust a comment), and that `run_order` interleaves.
`test_report.py`: the block reports `idle_slot_s` per arm and mixed over uniform.

**Prose.** Fast and slow task bodies in `benchmarks/tasks.py`, duration passed by the
stage. The mixed backlog is preloaded as a spread so slow tasks are not clustered; the
uniform length is derived from the mixed mean. Arms go through
`record_interleaved_measurements`; metrics come from task 1.

**Verify.** Real run on mains under `caffeinate -is`; record `idle_slot_s` per arm in
`benchmarks/CLAUDE.md`, naming the run, and say whether the mixed arm shows idle slots
against a backlog that still held work.

## Task 3 — `parked_runs`

**RED.** `test_cli.py`: drive `parked_runs` tiny; assert the three arm names and that
each sleepers arm recorded `sleeping_min` equal to the parked count — a broken park
leaves it 0, which is the positive observable that matters.

**Prose.** Sync and async bodies calling `context.sleep_for` past any window, plus the
quick body for the drain. The stage parks N sleepers, waits until their runs report
`sleeping`, then drains the quick batch on one worker while counting the sleepers' runs
by state on the harness's own connection. Warm up before the clock starts.

**Verify.** Real run; record the ratio and `running_max`/`sleeping_min`, reproducing or
contradicting `running_max=0`. A sleeper ever `running` inside the window is a finding,
not a broken test.

## Task 4 — `admin_at_volume`

**RED.** `test_cli.py`: drive it against a small seed; assert the arms for tasks and
runs, and that every arm records a `result_count` above zero and a non-empty plan.
`test_report.py`: the block renders wall ms, query count and the plan shape per arm.

**Prose.** Seed with `seed.py`. Timed pass first: drive the admin URLs through Django's
test client with `setup_test_environment()` called explicitly, reading
`TemplateResponse.context_data` for the result count. Capture pass second, on the same
URLs, for query count and the two plans — separate so no debug cursor touches a timed
number. Plans embedded in the results file.

**Verify.** Real run at a seed the 4 GB tmpfs holds; record timings and plan SHAPES.

## Task 5 — retire `loadtest/`

Only once tasks 2-4 have recorded real runs. Write the parity table into
`benchmarks/CLAUDE.md` as fact, and say in `docs/HISTORY.md` that `loadtest/` is
superseded, naming the branch its history lives on. Deleting the remote branch is an
outward action needing explicit sign-off; the doc change is not.

## Departed from this plan, and why

Kept as approved above; what actually happened where it differs.

- **Task 1's tests live in `test_metrics.py`, not `test_analysis.py`** — that is the
  file that already inserts hand-timed rows and asserts `analysis` over them.
- **`mean_busy` and `span_s` are not computed at all**, where the plan had them internal
  to the reducer. One figure is returned; a drain's span is already `phase_s`.
- **`idle_slot_s` is capped at `min(free, waiting)`.** The first implementation charged
  every free slot whenever anything waited, which contradicted the spec's own "wanted"
  and was not comparable with `loadtest`'s occupancy. Both original tests had
  `free == waiting` and could not tell the two apart; a third pins the difference.
- **Task 4 calls no `setup_test_environment()`.** It refuses to run twice, so a stage
  calling it works under `python -m stages` and raises under pytest. The real gap was a
  missing `ALLOWED_HOSTS`, without which a DEBUG-off admin request answers 400.
- **Task 4 makes three requests an arm, not two** — a discarded warm-up ahead of the
  timed one, as `loadtest` did. Without it the deep page's first-ever request read 717
  ms against 380 for its next two, and the arm's cv was 39% rather than 6%.
- **Both seeding stages truncate on the way out**, which no task here asked for. The
  first end-to-end run of all fourteen stages was killed for memory with a million
  `cleanup_vs_size` rows still resident in a tmpfs data directory.
- **The end-to-end run is not in this plan either**, and it earned its place: ten stages
  in 112 minutes, then killed, which is what found the truncate and corrected a README
  timing claim that had been extrapolated rather than measured.

## Order and cost

1 -> 2 -> 3 -> 4 -> 5. ~1 h for task 1, ~1 h each for 2 and 3, ~1-2 h for 4, minutes for
5, plus a real run each. Stop after any task; each leaves the harness green and the
branch commitable.
