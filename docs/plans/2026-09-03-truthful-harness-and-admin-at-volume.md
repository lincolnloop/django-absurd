# Truthful harness, and the admin at volume — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every number `benchmarks/` publishes traces to one dated defect-free run, and
a person can click through the admin at millions of rows.

**Architecture:** Land three existing commits plus a flake fix on one branch. Build the
two probes the run still needs — suspension, clone seeder. Take one full run. Rewrite
the findings from it. Then a dev-only Django project, seeded, clickable.

**Tech Stack:** Python 3.12+, Django 6.0+, psycopg3, Postgres 18, pytest + pytest-xdist,
tox, docker compose.

**Spec:** `docs/specs/2026-09-03-truthful-harness-and-admin-at-volume.md`

## Global Constraints

- Floor Django 6.0 / Python 3.12. psycopg (v3) backend only.
- `import typing as t` always. Never `from typing import X`. Absolute imports only.
- Functions contain a verb. No leading-underscore module constants or helpers. Helpers
  go BELOW their callers.
- Comments answer "why this, not the obvious alternative", ≤2 lines. Never narrate
  history or prior state.
- Docs and docstrings state what IS. No "previously", "now", before/after framing.
- Function-based pytest tests only, never class-based.
- **Never** add a ruff ignore, `noqa`, or `# pragma: no cover`. Fix the code or ask.
- **Never** add AI attribution to a commit. **Never** `git commit --amend`. **Never**
  bare `git stash` — the stash stack is shared across worktrees; use a WIP commit.
- Assert positive observables. Never assert absence.
- Ports in this worktree: `export PGPORT=5452 PGPORT_PGCRON=5453 PGPORT_BENCH=5460`.
  Required on every command touching Postgres, including `docker compose up`.
- No `timeout`/`gtimeout` on this machine. Bounded waits are a Python poll loop.
- Suites take `-n` explicitly: `uv run pytest tests/benchmarks -n4`. Not in any addopts.
- Gates before a commit: `uvx --with tox-uv tox -e dev,bench_harness` and
  `uv run pre-commit run --all-files`. `git add -A` BEFORE pre-commit — `--all-files`
  skips untracked. Never invoke ruff or mypy directly.
- Codecov gates MERGED coverage at 100%, project + patch.

---

## File structure

| File                          | Responsibility                                             |
| ----------------------------- | ---------------------------------------------------------- |
| `benchmarks/tasks.py`         | Task bodies. Gains a suspending sleeper, sync and async.   |
| `benchmarks/stages.py`        | Stage registry + builders. Gains `slot_occupancy`.         |
| `benchmarks/measurement.py`   | `MeasurementSpec` and the rep loop. Gains sleeper preload. |
| `benchmarks/analysis.py`      | SQL-side reads. Gains a sleeper-run state count.           |
| `benchmarks/report.py`        | Rendering. Gains the occupancy block.                      |
| `benchmarks/seed.py`          | NEW. Template → clone → `ANALYZE`, plus the drift guard.   |
| `benchmarks/admin_at_volume/` | NEW, Phase 4. Dev-only Django project + compose service.   |
| `tests/benchmarks/`           | Tiny-N CI coverage for every one of the above.             |

Phase 4 is separable — its own branch, and it would stand as its own plan if you would
rather split it. Kept here because it shares the seeder.

---

### Task 1: Consolidate the three existing commits

**Files:**

- Modify: whole tree via cherry-pick

**Interfaces:**

- Produces: `run_durable_work(seconds, touches)` in `benchmarks/tasks.py`;
  `probe_shape_backends` in `benchmarks/stages.py`; `size_vs_depth` stage;
  `benchmarks/workload/` app; `tests/core/test_claim_lease.py`.

- [ ] **Step 1: Confirm the branch base**

Run: `/usr/bin/git log --oneline -1` in the `pr-257` worktree. Expected: the branch is
`benchmarks__truthful-harness` at `origin/main`.

- [ ] **Step 2: Cherry-pick, oldest first**

```bash
/usr/bin/git cherry-pick 1286bf1   # durable workload, C+2 probe, concurrent spawn
/usr/bin/git cherry-pick c516581   # size_vs_depth stage
/usr/bin/git cherry-pick 1c9a8dd   # claim-lease tests
```

Cherry-pick, not merge: `benchmarks__size-vs-depth` was cut off
`benchmarks__durable-workload`, so merging both drags `1286bf1` through twice. Resolve
any conflict in favour of the later commit's version of `stages.py`, then re-read the
surrounding function to check the merge left it coherent.

- [ ] **Step 3: Run both suites**

Run: `PGPORT=5452 uv run pytest tests/benchmarks -n4` then
`PGPORT=5452 uv run pytest tests/core -n4` Expected: PASS. `tests/benchmarks` is 113 on
this branch, `tests/core` 566+.

- [ ] **Step 4: Commit nothing**

Cherry-picks already committed. Do not squash them; each carries its own reasoning.

---

### Task 2: Loosen the two CI-flaky assertions

**Files:**

- Modify: `tests/benchmarks/test_smoke.py` (rate-ramp test ~:298, commit-ceiling ~:780)

**Interfaces:**

- Consumes: `stages.measure_sustainable_rate`, `analysis.measure_commit_ceiling`,
  `stages.RATE_RAMP_START_FRACTION`, `analysis.PROBE_WARM_UP_COMMITS`,
  `analysis.PROBE_TIMED_ROUNDS`, `analysis.DURABLE_PROBE_COMMITS`.

Both tests pass locally every time and failed on a CI runner on a markdown-only commit —
environment sensitivity, not regression. Roughly 1 in 2.

**Failure 1**, `test_rate_ramp_measures_at_the_highest_offer_it_absorbed`: key
`climbed_before_it_refused` was False. The ramp refused its FIRST rung, so `absorbed`
came back empty. `RAMP_CEILING_PER_S = 900.0` is fixed and `RATE_RAMP_START_FRACTION` of
it exceeded what a 2-worker fleet on that runner could absorb.

The bind: the test needs the ramp to absorb ≥1 rung AND then refuse one. Absorb
everything and the length check fails; absorb nothing and `climbed_before_it_refused`
fails. A fixed ceiling cannot put both a fast workstation and a slow shared runner
inside that window.

**Failure 2**, `test_commit_ceiling_probe_times_a_warmed_session_not_a_cold_one`: key
`timed_only_the_rounds_it_kept` was False. Check is `timed_s < 0.75 * elapsed_s`. Timed
rounds are nominally a bit over half the probe's commits, so the honest ratio sits near
0.55; runner noise inside the timed rounds pushes it past 0.75. A probe that summarized
its warm-up too lands near 1.0, so headroom exists between honest and broken.

- [ ] **Step 1: Reproduce the reasoning, not the failure**

A slow shared runner cannot be reproduced on this machine. Read both tests and confirm
the two diagnoses above against the source before changing anything. If either reading
is wrong, stop and report.

- [ ] **Step 2: Make the ramp's ladder straddle real capacity**

Derive the ramp's ceiling from a ceiling the machine demonstrates — a short real drain —
rather than the fixed 900.0, so the first rung is absorbable and the top rung is beyond
reach by construction. This is what the production stage already does: it reads the
drain ceiling off `stage_process_scaling.json`. The sibling test above already covers
refuse-everything via `UNABSORBABLE_CEILING_PER_S`, so this test need not.

If a simpler construction makes the straddle robust, take it and say why in the commit.

- [ ] **Step 3: Raise the commit-ceiling threshold to the loosest value that still fails
      the mutant**

Update the docstring, which already carries this reasoning, to match the number chosen.
The docstring states what IS — no before/after framing.

- [ ] **Step 4: Prove both guards still bite**

For EACH test: mutate production code in `benchmarks/` so the guarded property breaks,
run the test, confirm FAIL, revert, confirm PASS. Record the exact mutation and result
in the commit body. A loosened assertion you cannot break is worse than the flake.

- [ ] **Step 5: Run the suite three times**

Run: `PGPORT=5452 uv run pytest tests/benchmarks -n4` ×3 Expected: PASS each time. Three
runs because the thing being fixed is intermittent.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: give the ramp and ceiling probes tolerances a shared runner can meet"
```

---

### Task 3: Make `bench_harness` a required check

**Files:**

- Modify: GitHub ruleset `18038740` on `lincolnloop/django-absurd` (no repo files)

**Interfaces:**

- Consumes: the `bench_harness` job added to `.github/workflows/test.yml` on `main`.

Fifteen checks are required today, including `dev`. `bench_harness` becomes the
sixteenth.

- [ ] **Step 1: Confirm Task 2 landed and is green**

The ordering is the point: requiring this check at a 1-in-2 flake rate blocks the very
PRs that fix it. Do not start this task before Task 2 is merged.

- [ ] **Step 2: Read the current required-check list**

```bash
gh api repos/lincolnloop/django-absurd/rulesets/18038740 \
  -q '.rules[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
```

- [ ] **Step 3: Add the context, preserving every existing entry**

PATCH the ruleset with the existing list plus `bench_harness`. Read the response back
and diff it against Step 2's output plus the one addition. A dropped entry here silently
removes a merge gate.

- [ ] **Step 4: Verify on a live PR**

Confirm `bench_harness` shows as required on an open PR before considering this done.

---

### Task 4: A suspending sleeper task, sync and async

**Files:**

- Modify: `benchmarks/tasks.py`
- Test: `tests/benchmarks/test_smoke.py`

**Interfaces:**

- Produces: `sleep_durably(seconds)` and `sleep_durably_async(seconds)` in
  `benchmarks/tasks.py`. Both suspend via the Absurd context's durable sleep rather than
  blocking. Distinct from the existing `sleep_sync` / `sleep_async`, which block on
  purpose.

Nothing in `benchmarks/` suspends today. Every body is a blocking sleep, an `asyncio`
sleep, a noop, or a `ctx.step` workflow. The async twin is required, not optional: only
the sync path crosses the `--concurrency`-sized thread pool, so a finding without both
cannot say whether the bridge or the loop is responsible.

- [ ] **Step 1: Write the failing test**

```python
def test_a_durable_sleeper_leaves_its_run_sleeping_not_running(tmp_path):
    """A suspended task holds no claim, so it occupies no worker slot.

    Asserted on the run's own state rather than on a timing: `sleeping` is the
    state that cannot coexist with a held claim, so it is the observable that
    can only be true if the sleep released the slot.
    """
    task_id = tasks.sleep_durably.enqueue(seconds=30.0)
    with running_worker(concurrency=1):
        wait_until_state(task_id, "sleeping", timeout_s=20.0)

    assert count_runs_by_state(task_id) == {"sleeping": 1}
```

- [ ] **Step 2: Run it and watch it fail**

Run:
`PGPORT=5452 uv run pytest tests/benchmarks/test_smoke.py::test_a_durable_sleeper_leaves_its_run_sleeping_not_running -v`
Expected: FAIL — `tasks.sleep_durably` does not exist.

- [ ] **Step 3: Add both task bodies**

Add the sync body, taking a duration and suspending for it through the Absurd context's
durable sleep. Add the async twin the same way. Register both on the harness's `bench`
queue alongside the existing bodies. Keep the duration a parameter so the CI test can
pass seconds and a real run can pass minutes.

Follow the naming rule — the verb is in the name. Do not reuse `sleep_sync`; that body
blocks deliberately and its comment says so.

- [ ] **Step 4: Run it and watch it pass**

Run: same command as Step 2. Expected: PASS.

- [ ] **Step 5: Add the async twin's test**

Same assertion against `sleep_durably_async`. Two tests, not one parametrized over both
— the sync case crosses the thread pool and the async case does not, so a shared failure
message would hide which path broke.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: add durable sleepers, sync and async"
```

---

### Task 5: The `slot_occupancy` stage

**Files:**

- Modify: `benchmarks/stages.py`, `benchmarks/measurement.py`, `benchmarks/analysis.py`,
  `benchmarks/report.py`
- Test: `tests/benchmarks/test_cli.py`, `tests/benchmarks/test_report.py`

**Interfaces:**

- Consumes: `tasks.sleep_durably`, `tasks.sleep_durably_async` from Task 4.
- Produces: stage name `slot_occupancy` in `stages.STAGE_NAMES` and
  `stages.STAGE_DESCRIPTIONS`; `analysis.count_sleeper_runs_by_state(queue, task_ids)`
  returning a mapping of state to count; report keys `running_max` and `sleeping_min`.

**The question.** A sync task's durable sleep hops to the worker's event loop while the
body sits in a thread of a pool sized to `--concurrency`. If that thread stayed parked,
N sleepers would hold N of C slots and everything behind them starves. The docs say the
task suspends durably and resumes later. That is the claim under test, not a premise.

**Shape.** Two arms per workload, run one at a time so they never measure each other.
`control` drains a fixed quick batch on one worker with no sleepers. `sleepers` drains
the same batch on the same worker with N tasks already suspended in a sleep far longer
than the window. Both arms pay the same worker start-up and the same warm-up task before
the clock starts. The ratio is the finding: near 1 means the sleep released its slot. An
arm that never drains fails the run rather than recording a number.

- [ ] **Step 1: Write the failing CLI test**

```python
def test_slot_occupancy_reports_both_arms_and_a_state_count(tmp_path):
    """The ratio is only trustworthy beside a count that needs no clock.

    A timing says the quick batch was not slowed; the state count says why —
    the sleepers held no claims. Asserted as a shape so a machine's own speed
    never decides whether this passes.
    """
    written = run_stage_cli("slot_occupancy", tmp_path, tasks=60, sleepers=4)
    result = json.loads(written.read_text())

    arms = {arm["name"]: arm for arm in result["arms"]}
    assert set(arms) == {"control", "sleepers", "control_async", "sleepers_async"}
    assert {
        "every_arm_drained": all(arm["missing_tasks"] == 0 for arm in arms.values()),
        "every_arm_ran_the_batch": all(
            arm["n_tasks"] == 60 for arm in arms.values()
        ),
        "sleepers_were_counted": arms["sleepers"]["sleeping_min"] == 4,
        "no_sleeper_held_a_claim": arms["sleepers"]["running_max"] == 0,
    } == {
        "every_arm_drained": True,
        "every_arm_ran_the_batch": True,
        "sleepers_were_counted": True,
        "no_sleeper_held_a_claim": True,
    }
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_cli.py -k slot_occupancy -v`
Expected: FAIL — `slot_occupancy` is not a stage choice.

- [ ] **Step 3: Add the state count to `analysis.py`**

A function that, given the queue and the sleepers' task ids, aggregates their runs by
state. `running` holds a claim and therefore a slot; `sleeping` holds neither. Return
the mapping; let the caller decide what is worst.

- [ ] **Step 4: Teach `measurement.py` to preload sleepers**

A spec field for the sleeper count, defaulting to none so no existing measurement
changes behaviour. When set, enqueue that many durable sleepers and wait until every one
of them reports `sleeping` BEFORE the measured batch is enqueued — a sleeper still
starting up would otherwise be counted as occupying a slot it is about to release.

Sample the state count while the quick batch drains, keeping the worst `running` and the
least `sleeping` seen.

- [ ] **Step 5: Add the stage builder**

Four arms — `control`, `sleepers`, `control_async`, `sleepers_async` — run sequentially.
Register the name and a one-line description in the registry beside the existing stages.

- [ ] **Step 6: Render it**

An occupancy block in the report: per arm the elapsed time, the enqueue share, the ratio
against its own control, and the two state counts. Print the ratio's meaning next to it
in one line, the way the other stages' derived lines read.

- [ ] **Step 7: Run the tests**

Run: `PGPORT=5452 uv run pytest tests/benchmarks -n4` Expected: PASS, including a report
test asserting the rendered block.

- [ ] **Step 8: Prove the guard bites**

Mutate the preload so it does not wait for sleepers to reach `sleeping`, and confirm the
CLI test fails. Revert. Record it in the commit body.

- [ ] **Step 9: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: measure whether a durable sleep releases its worker slot"
```

---

### Task 6: The clone seeder and its drift guard

**Files:**

- Create: `benchmarks/seed.py`
- Test: `tests/benchmarks/test_seed.py`

**Interfaces:**

- Produces: `seed_queue_tables(rows, *, queue)` returning a summary of what it wrote —
  task count, run count, elapsed seconds; and `check_queue_table_shape()`, which raises
  when the live columns differ from what the clone writes.

Volume by enqueueing one task at a time does not reach millions. The archived clone
wrote template rows through the real API then cloned them server-side in SQL: 250k
tasks + 375k runs in 21 s.

**The drift guard is the load-bearing part.** Cloning writes queue tables directly, so
it encodes their shape. When upstream changes a column the clone must fail loudly rather
than write plausible-looking wrong rows. A silent drift here poisons every number taken
on seeded data.

- [ ] **Step 1: Write the drift guard's failing test first**

```python
def test_seeding_refuses_a_queue_table_whose_columns_it_does_not_know(monkeypatch):
    """The guard fails the seed, not the read.

    A clone that writes a table it half-understands produces rows that look
    right and are not, so the only safe failure is at the seed. Asserted by
    naming the offending column, since a bare refusal would not tell the next
    reader which upstream change moved.
    """
    monkeypatch.setattr(seed, "CLONED_TASK_COLUMNS", ("task_id", "not_a_column"))

    with pytest.raises(seed.QueueTableShapeError) as caught:
        seed.check_queue_table_shape()

    assert "not_a_column" in str(caught.value)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_seed.py -v` Expected: FAIL — no
`benchmarks/seed.py`.

- [ ] **Step 3: Write the guard**

Read the live columns for each queue table from `information_schema`, compare against
the tuples the clone writes, and raise a typed error naming every column that is
expected and absent. The error owns its message; the caller assembles no text. Name the
error for the condition.

- [ ] **Step 4: Write the seeding test**

```python
def test_seeding_clones_templates_into_the_row_count_it_was_asked_for():
    """Rows are counted from the tables, not from the seeder's own bookkeeping.

    A seeder that returns its intended count rather than its written count
    reports success for a clone that silently wrote nothing.
    """
    summary = seed.seed_queue_tables(rows=200, queue="bench")

    assert {
        "reported": summary["tasks"],
        "actually_in_the_table": count_tasks_in_queue("bench"),
    } == {"reported": 200, "actually_in_the_table": 200}
```

- [ ] **Step 5: Implement seeding**

Write a small number of template rows through the real enqueue API so their shape is
whatever the library actually writes. Clone them server-side with `generate_series`,
giving each clone a fresh `uuidv7()` primary key so pk order stays chronological.
`ANALYZE` both queue tables afterwards, because a bulk-loaded table with stale
statistics gives the planner a row count that is orders out and every plan taken on it
is a different plan.

Call the guard before writing anything.

- [ ] **Step 6: Run the tests**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_seed.py -v` Expected: PASS.

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: seed the queue tables by cloning templates"
```

---

### Task 7: Take the run

**Files:**

- Create: `benchmarks/results/<dated dir>/` (git-ignored output)

**Interfaces:**

- Consumes: every stage, including `size_vs_depth` and `slot_occupancy`.
- Produces: the JSON and report files Phase 3 rewrites the docs from.

Not a coding task. A procedure, run once, whose output is the evidence for every claim
the docs then make.

- [ ] **Step 1: Confirm the tree has no known defect**

Tasks 1–6 merged. `tox -e dev,bench_harness` green. Any measurement defect still
documented as unfixed in `benchmarks/CLAUDE.md` is either fixed now or this run cannot
close it — check the list before spending 90 minutes.

- [ ] **Step 2: Start the tuned server**

```bash
PGPORT_BENCH=5460 docker compose --profile bench up -d --wait db_bench
```

- [ ] **Step 3: Quiet the machine and hold it awake**

On mains, not battery. Close everything else. Prefix the run with `caffeinate -is`. This
machine has slept 25–284 s mid-run before; `perf_counter` does not count the nap, so a
napped arm reads fast and entirely plausible.

- [ ] **Step 4: Run every stage**

Run with `--durable-seconds 30` — a realistic agent tool call, not the 2 s CI default.
Expect roughly 90 minutes. Do not touch the machine.

- [ ] **Step 5: Stamp the environment beside the results**

Record the machine, core count, power source, Postgres version and settings, the
`--durable-seconds` used, and the commit SHA. A number without its environment is not
reproducible and cannot be compared with the next run.

- [ ] **Step 6: Check the run before trusting it**

Per arm: cv, whether every rep drained, whether any rep was invalidated for extra runs.
An arm with wide cv is reported as wide, never averaged into confidence. If the
commit-ceiling probe refused, the report says so and that arm's connection-bound verdict
is unavailable rather than guessed.

- [ ] **Step 7: Commit nothing**

Results are git-ignored. The findings land in Task 8.

---

### Task 8: Restate every finding from that run

**Files:**

- Modify: `benchmarks/CLAUDE.md`, `benchmarks/README.md`

**Interfaces:**

- Consumes: Task 7's results directory and environment stamp.

- [ ] **Step 1: Write the `checkpoint_cost` finding, which does not exist**

The stage has run since it was built and `benchmarks/CLAUDE.md` carries no finding for
it. It is the stage whose shape most resembles durable agent work — a `ctx.step` per
tool call. State what the run measured and what it means for a workflow that checkpoints
per step.

- [ ] **Step 2: Replace every multi-process figure**

Every rate measured before the spawn fix is not comparable with one measured after.
Replace rather than annotate. `README.md` is for a non-expert human and describes
actions; `CLAUDE.md` carries the jargon, the schemas and the evidence.

- [ ] **Step 3: Answer the three open questions in prose**

Whether processes-beat-threads survives a durable body; whether the 2.45x is table size
or queue depth; whether a durable sleep releases its slot. Each with the number, the
method, and what it does not support.

- [ ] **Step 4: Delete defect notes for defects that are gone**

The spawn-bias note and its 7-15% estimate describe a fixed defect. Documentation states
what IS. A fixed defect described as live is a false claim about the current tree.

Keep the `process_scaling` ladder confound — it is still real, still confounded, and the
report still prints `CONFOUNDED:`.

- [ ] **Step 5: Name the run on every finding**

Each figure carries its dated run. A number with no run is a defect a reader can catch.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "docs: restate the harness findings from a defect-free run"
```

---

### Task 9: The admin-at-volume stack

**Files:**

- Create: `benchmarks/admin_at_volume/{settings.py,urls.py,manage.py,README.md}`
- Modify: `compose.yaml`
- Test: `tests/benchmarks/test_admin_at_volume.py`

**Interfaces:**

- Consumes: `benchmarks/seed.py` from Task 6.
- Produces: a `db_admin` compose service on a real data directory behind an `admin`
  profile; a Django project whose admin serves django-absurd's auto-registered queue
  models.

Its own branch, off `main` after Task 8 merges. New surface — a project, a service, a
seeder consumer — and bundling would hold the harness fixes hostage.

**Placement.** Dev-only under `benchmarks/`, not shipped in any wheel, not in the
examples CI matrix. `examples/web` was considered and rejected: it is a tutorial whose
README is a walkthrough and whose suite is a required check, and a volume seeder does
not belong in a teaching example.

**Persistence.** A real data directory, not tmpfs. A seeded corpus that evaporates on
restart is worse than no corpus. Behind a profile so a bare `up -d` leaves it alone, the
way `db_bench` already is.

- [ ] **Step 1: Write the failing test**

```python
def test_the_admin_changelist_serves_a_seeded_corpus(admin_client):
    """One row on the page proves the whole stack: settings, URLs, admin
    registration, and the seeder's rows all have to be right for this to pass.

    Asserted on a row the seeder wrote, so a changelist that renders an empty
    table cannot pass.
    """
    seed.seed_queue_tables(rows=200, queue="bench")

    response = admin_client.get(reverse("admin:django_absurd_task_changelist"))

    assert response.status_code == 200
    assert response.context_data["cl"].result_count == 200
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_admin_at_volume.py -v` Expected:
FAIL — no settings module.

- [ ] **Step 3: Write the project**

Settings deriving from the harness's existing settings so the connection cannot drift,
plus the admin app and its dependencies. A URLconf mounting the admin. A `manage.py`.
Keep it one queue and no custom models — the point is django-absurd's own admin under
volume.

- [ ] **Step 4: Add the compose service**

`db_admin` on a named volume, behind an `admin` profile, on its own published port with
an env-var default like the others.

- [ ] **Step 5: Run the test**

Expected: PASS at 200 rows, which is what CI will run.

- [ ] **Step 6: Write the README**

For a person, not an agent: start the service, seed N rows, create a superuser, run the
server, open the admin. Every command copy-pasteable. State that the corpus is synthetic
— uniform ages, a synthetic `claimed_by` spread — so nobody quotes a number from it as a
property of the library.

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: add a dev-only admin stack for browsing volume"
```

---

### Task 10: The admin probe

**Files:**

- Create: `benchmarks/admin_at_volume/probe.py`
- Test: `tests/benchmarks/test_admin_at_volume.py`

**Interfaces:**

- Consumes: the Task 9 stack and `benchmarks/seed.py`.
- Produces: `probe_admin_arms(arms)` writing a JSON summary per arm — cold ms, warm ms,
  query count — and an `EXPLAIN (ANALYZE, BUFFERS)` dump per arm to a file.

Arms cover the changelists that order by pk and at least one filtered view: a filter
changes the plan, and ordering by pk does not help it.

- [ ] **Step 1: Write the failing test**

```python
def test_the_probe_records_a_query_count_and_a_plan_for_every_arm(tmp_path):
    """A timing alone cannot say why a page was slow, so an arm without a plan
    is not a finding.

    Asserted on the artefacts rather than the durations: durations are this
    machine's to decide, but every arm owing a plan file and a query count is
    the probe's own contract.
    """
    seed.seed_queue_tables(rows=200, queue="bench")

    summary = probe.probe_admin_arms(results_dir=tmp_path)

    assert [arm["name"] for arm in summary["arms"]] == [
        "tasks_first_page",
        "tasks_filtered",
        "runs_first_page",
    ]
    assert all(
        arm["queries"] > 0 and (tmp_path / f"explain_{arm['name']}.txt").exists()
        for arm in summary["arms"]
    )
```

- [ ] **Step 2: Run it and watch it fail**

Expected: FAIL — no `probe.py`.

- [ ] **Step 3: Implement the probe**

Per arm: issue the request twice, keeping the first as cold and the second as warm;
capture the queries the request ran; and dump the changelist query's plan with `ANALYZE`
and `BUFFERS` to a per-arm file. Write one JSON summary beside them.

Take timings from a real request through the stack rather than from the ORM alone —
template rendering of 100 rows is part of what a person waits for.

- [ ] **Step 4: Run the test**

Expected: PASS.

- [ ] **Step 5: Assert the plan shape at volume**

Add one test asserting the tasks changelist plan uses an index scan backward on the pkey
with no sort node. This is the one thing about the merged ordering fix that has never
been observed at volume, and it is a plan-shape claim, so it needs no timing.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: time the admin changelists and keep their plans"
```

---

## Self-review

**Spec coverage.** Truthful-harness criteria → Tasks 1, 2, 7, 8. `checkpoint_cost`
finding → Task 8 Step 1. Five questions → Tasks 5, 7, 8. Seeder + drift guard → Task 6.
Suspension probe with async twin and state count → Tasks 4, 5. Admin clickable + README
→ Task 9. Admin timings + `EXPLAIN` → Task 10. Rot control via `tests/benchmarks` →
every task's test. Branch topology → Task 1 and Task 9's preamble. Required check →
Task 3.

**Gap found and left deliberate:** the spec defers retry storms, cleanup keep-up, suite
speed, the `worker_knobs` durable arm and multi-queue routing. No tasks, by design.

**Gap found and closed:** the spec's success criterion "no measurement defect documented
as unfixed" needed a step that deletes the stale spawn-bias note while keeping the
still-real `process_scaling` confound. Task 8 Step 4.

**Placeholders:** none. Every code step is either a test to write or prose describing
the minimal implementation — production code is deliberately not pre-written, per the
project's TDD rule.

**Type consistency:** `seed.seed_queue_tables` / `seed.check_queue_table_shape` /
`seed.QueueTableShapeError` / `seed.CLONED_TASK_COLUMNS` consistent across Tasks 6,
9, 10. `tasks.sleep_durably` / `sleep_durably_async` consistent across Tasks 4, 5.
`analysis.count_sleeper_runs_by_state` used only in Task 5. `running_max` /
`sleeping_min` consistent between Task 5's test and its report step.
`probe.probe_admin_arms` consistent within Task 10.
