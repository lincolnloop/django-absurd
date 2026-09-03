# Truthful harness, and the admin at volume — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every multi-process number `benchmarks/` publishes traces to one dated run on
a tree with no known measurement defect, and a person can click through the admin at
millions of rows.

**Architecture:** Land three existing commits, the flake fix, the windowing fix, the
clone seeder and one suspension test on a single harness branch. Merge. Take one run.
Restate the findings from it on a second branch. Then a dev-only admin project on a
third.

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
- **Never** monkeypatch. `tests/CLAUDE.md` allows exactly one carve-out and this work is
  not it. Drive a condition through the real entrypoint instead.
- **Never** unit-test an internal helper. Tests go through public entrypoints.
- **Never** add AI attribution to a commit. **Never** `git commit --amend`. **Never**
  bare `git stash` — the stash stack is shared across worktrees; use a WIP commit.
- Assert positive observables. Never assert absence, including "no sort node".
- Any test whose worker children must see committed rows needs
  `pytest.mark.django_db(transaction=True)` — children cannot see an open transaction's
  writes.
- Ports in this worktree: `export PGPORT=5452 PGPORT_PGCRON=5453 PGPORT_BENCH=5460`.
  Required on every command touching Postgres, including `docker compose up`.
- No `timeout`/`gtimeout` on this machine. Bounded waits are a Python poll loop.
- Suites take `-n` explicitly: `uv run pytest tests/benchmarks -n4`. Not in any addopts.
- Gates before a commit: `uvx --with tox-uv tox -e dev,bench_harness` and
  `uv run pre-commit run --all-files`. `git add -A` BEFORE pre-commit — `--all-files`
  skips untracked. Never invoke ruff or mypy directly.
- Codecov gates MERGED coverage at 100%, project + patch. `[tool.coverage.run] source`
  is `["benchmarks", "django_absurd", "tests"]`, and unexecuted files under those roots
  are scanned in — so a new module no test imports lands at 0% and turns the patch
  status red.

---

## File structure

| File                          | Responsibility                                                       |
| ----------------------------- | -------------------------------------------------------------------- |
| `benchmarks/measurement.py`   | `MeasurementSpec` and the rep loop. Takes the post-fleet mark.       |
| `benchmarks/analysis.py`      | SQL-side reads. Already accepts a `since` mark.                      |
| `benchmarks/seed.py`          | NEW. Templates → drain → clone → `ANALYZE`, plus the drift guard.    |
| `benchmarks/admin_at_volume/` | NEW, task 9. Seeder consumer + probe. No settings module of its own. |
| `tests/benchmarks/`           | Tiny-N CI coverage for the seeder, the windowing and the probe.      |
| `tests/core/`                 | The suspension test. It is a library behaviour, not a harness one.   |

---

### Task 1: Consolidate the three commits and delete the doc section they falsify

**Files:**

- Modify: whole tree via cherry-pick; `benchmarks/CLAUDE.md`

**Interfaces:**

- Produces: `run_durable_work(seconds, touches)` in `benchmarks/tasks.py`;
  `probe_shape_backends` in `benchmarks/stages.py`; the `size_vs_depth` stage;
  `analyze_saturation(queue, since)` in `benchmarks/analysis.py`; the
  `benchmarks/workload/` app; `tests/core/test_claim_lease.py`.

- [ ] **Step 1: Cherry-pick, oldest first**

```bash
/usr/bin/git cherry-pick 1286bf1   # durable workload, backend probe, concurrent spawn
/usr/bin/git cherry-pick 1c9a8dd   # claim-lease tests
/usr/bin/git cherry-pick c516581   # size_vs_depth stage
```

`benchmarks__size-vs-depth` was cut off `benchmarks__durable-workload`, so merging both
would drag `1286bf1` through twice. On a conflict in `stages.py`, take the later
commit's version, then re-read the whole surrounding function to confirm the result is
coherent rather than merely conflict-free.

- [ ] **Step 2: Delete the section the concurrent-spawn commit falsifies**

`benchmarks/CLAUDE.md` describes `start_workers` spawning children one at a time and
blocking on each readiness line. After `1286bf1` it also says the fleet starts all at
once. Delete the stale description. Documentation states what IS, so leaving both is a
contradiction, not history.

Keep the `commits_per_task` / `calls_per_task` note. That defect is still real — Task 3
fixes it, and Task 8 removes the note once a run proves it gone.

- [ ] **Step 3: Run both suites**

Run: `PGPORT=5452 uv run pytest tests/benchmarks -n4` then
`PGPORT=5452 uv run pytest tests/core -n4` Expected: PASS.

- [ ] **Step 4: Commit the doc deletion only**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "docs: describe one fleet start-up, not two"
```

---

### Task 2: Give the flaky assertions tolerances a shared runner can meet

**Files:**

- Modify: `tests/benchmarks/test_smoke.py`

**Interfaces:**

- Consumes: `stages.measure_sustainable_rate`, `analysis.measure_commit_ceiling`,
  `stages.RATE_RAMP_START_FRACTION`, `analysis.PROBE_WARM_UP_COMMITS`,
  `analysis.PROBE_TIMED_ROUNDS`, `analysis.DURABLE_PROBE_COMMITS`.

Observed once, on CI run `33710633257`, job `dev`, on a commit that changed only
markdown — environment sensitivity, not a regression. One failure against eleven
successes in the last twelve `test.yml` runs, so this is rare rather than routine; it is
worth fixing because `bench_harness` becomes a required check in Task 6, not because it
fires often.

**Failure 1**, `test_rate_ramp_measures_at_the_highest_offer_it_absorbed`: key
`climbed_before_it_refused` was False — the ramp refused its FIRST rung, so `absorbed`
came back empty. `RAMP_CEILING_PER_S = 900.0` is fixed and `RATE_RAMP_START_FRACTION` of
it exceeded what a 2-worker fleet on that runner absorbed.

The bind: the test needs the ramp to absorb ≥1 rung AND then refuse one. Absorb
everything and the length check fails; absorb nothing and `climbed_before_it_refused`
fails. A fixed ceiling cannot put both a fast workstation and a slow shared runner
inside that window.

**Failure 2**, `test_commit_ceiling_probe_times_a_warmed_session_not_a_cold_one`: key
`timed_only_the_rounds_it_kept` was False. Check is `timed_s < 0.75 * elapsed_s`; the
honest ratio sits near 0.55 and runner noise pushed it past 0.75. A probe that
summarized its warm-up too lands near 1.0, so headroom exists between honest and broken.

- [ ] **Step 1: Confirm both diagnoses against the source**

A slow shared runner cannot be reproduced here, so this step is reading, not running. If
either diagnosis is wrong, stop and report rather than proceeding on it.

- [ ] **Step 2: Make the ramp's ladder straddle demonstrated capacity**

Derive the ceiling from a short real drain the machine performs, so the first rung is
absorbable and the top rung is beyond reach by construction — which is what the
production stage does, reading the drain ceiling off `stage_process_scaling.json`. The
sibling test above already covers refuse-everything via `UNABSORBABLE_CEILING_PER_S`.

- [ ] **Step 3: Raise the ceiling threshold to the loosest value that still fails the
      mutant**

Update the docstring to match the number chosen. It states what IS.

- [ ] **Step 4: Prove both guards still bite**

For EACH test: mutate production code in `benchmarks/` so the guarded property breaks,
run the test, confirm FAIL, revert, confirm PASS. Record both mutations in the commit
body.

- [ ] **Step 5: Run the suite three times**

Run: `PGPORT=5452 uv run pytest tests/benchmarks -n4` ×3 Expected: PASS each time.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: give the ramp and ceiling probes tolerances a shared runner can meet"
```

---

### Task 3: Window saturation reps on a mark taken after the fleet is up

**Files:**

- Modify: `benchmarks/measurement.py`
- Test: `tests/benchmarks/test_cli.py`

**Interfaces:**

- Consumes: `analyze_saturation(queue, since)` and the mark helper `c516581` added to
  `benchmarks/analysis.py` — read that commit for the exact names before writing.
- Produces: no new public name. Saturation reps pass a mark where they pass `None`
  today.

Concurrent spawn shrinks the stagger but does not remove it: children still reach
readiness a few hundred milliseconds apart, and the first one claims alone until the
last exists. `benchmarks/CLAUDE.md` documents `commits_per_task` and `calls_per_task`
reading low above one process for this reason and names windowing as the fix. `c516581`
already threads `since` through `analyze_saturation`, but only `size_vs_depth` passes
one, and it captures the mark BEFORE the fleet starts — excluding ballast, not stagger.

This is the smallest change in the plan and the only one that makes a trust condition
the spec already claims actually true.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.django_db(transaction=True)
def test_a_multi_process_rep_counts_only_commits_made_after_its_fleet_was_up(tmp_path):
    """Commits a worker makes while its siblings are still starting belong to
    no measured window.

    Asserted through commits_per_task, which is a ratio of counted commits to
    counted tasks: a rep that starts counting before the fleet is up counts a
    fraction of the commits against all of the tasks, so the ratio reads below
    the single-process value. Two processes are enough to produce a stagger.
    """
    stages.main(["process_scaling", "--results-dir", str(tmp_path), "--tasks", "400"])

    measurements = json.loads(
        (tmp_path / "stage_process_scaling.json").read_text()
    )["measurements"]
    by_count = {m["processes"]: m for m in measurements}

    assert (
        by_count[2]["commits_per_task"] >= by_count[1]["commits_per_task"] * 0.9
    ) is True
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_cli.py -k commits_made_after -v`
Expected: FAIL — the two-process ratio reads low.

If it PASSES at this small N, the stagger is not observable at 400 tasks on this
machine. Raise N until it fails, and if it will not fail at any N the suite can afford,
say so and stop: a test that cannot observe the defect must not be committed as if it
guards it.

- [ ] **Step 3: Take the mark after `start_workers` returns**

In the saturation rep, capture a database mark once the fleet is up and readiness is
confirmed, and pass it where `None` goes today. The preload already happens before the
fleet, so the mark must be captured after the fleet and before the drain is timed.

- [ ] **Step 4: Run it and watch it pass**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Confirm nothing else moved**

Run: `PGPORT=5452 uv run pytest tests/benchmarks -n4` Expected: PASS. `size_vs_depth`
already passes its own mark; check its tests still hold.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: count a rep's commits from the moment its fleet is up"
```

---

### Task 4: The clone seeder and its drift guard

**Files:**

- Create: `benchmarks/seed.py`
- Test: `tests/benchmarks/test_seed.py`

**Interfaces:**

- Produces: `seed_queue_tables(rows, *, queue)` returning a summary with `tasks`, `runs`
  and `elapsed_s`, counted from the tables themselves; `check_queue_table_shape()`,
  raising `QueueTableShapeError` when live columns differ from what the clone writes.

Volume by enqueueing one task at a time does not reach millions. Write template rows
through the real API, drain some of them so finished runs exist, then clone server-side.

**Two things the archive got right and are easy to lose.** First, the drift guard:
cloning writes queue tables directly, so it encodes their shape, and when upstream
changes a column it must fail loudly rather than write plausible-looking wrong rows.
Second, the drain: enqueueing writes `pending` tasks and NO finished runs, so a corpus
built by enqueueing alone leaves the runs table empty and the admin's runs changelist
with nothing to page.

- [ ] **Step 1: Write the drift guard's failing test**

```python
@pytest.mark.django_db(transaction=True)
def test_seeding_refuses_a_queue_table_whose_shape_it_does_not_know():
    """The guard fails the seed, not the read.

    A clone that writes a table it half-understands produces rows that look
    right and are not, so the only safe failure is before any row is written.
    Driven by really altering the table rather than by patching what the
    seeder believes, so the guard is tested against the condition it exists
    for. The error names the column so the next reader learns which upstream
    change moved.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("alter table absurd.t_bench drop column if exists params")

    with pytest.raises(seed.QueueTableShapeError) as caught:
        seed.seed_queue_tables(rows=10, queue="bench")

    assert "params" in str(caught.value)
```

The `_isolate_queues` fixture in `tests/conftest.py` hard-drops the schema before and
after, so the altered table does not leak. Apply it.

- [ ] **Step 2: Run it and watch it fail**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_seed.py -v` Expected: FAIL — no
`benchmarks/seed.py`.

- [ ] **Step 3: Write the guard**

Read the live columns for each queue table from `information_schema`, compare against
what the clone writes, and raise a typed error naming every column expected and absent.
The error owns its message — the caller assembles no text. Name it for the condition.
Call it before writing anything.

- [ ] **Step 4: Write the seeding test**

```python
@pytest.mark.django_db(transaction=True)
def test_seeding_writes_the_rows_it_reports_including_finished_runs():
    """Counted from the tables, not from the seeder's bookkeeping.

    A seeder returning its intended count reports success for a clone that
    silently wrote nothing. Runs are asserted separately because enqueueing
    alone produces none, and a corpus with an empty runs table cannot answer
    an admin question about the runs changelist.
    """
    summary = seed.seed_queue_tables(rows=200, queue="bench")

    assert {
        "tasks_reported": summary["tasks"],
        "tasks_present": count_rows("t_bench"),
        "runs_present_at_all": count_rows("r_bench") > 0,
    } == {"tasks_reported": 200, "tasks_present": 200, "runs_present_at_all": True}
```

`count_rows` is a new helper in `tests/benchmarks/utils.py`, beside the counting helpers
already there.

- [ ] **Step 5: Implement seeding**

Write a small number of template rows through the real enqueue API so their shape is
whatever the library actually writes. Drain them with a worker so finished runs exist,
and include at least one template that fails so retried and failed states appear. Then
clone server-side with `generate_series`, giving each clone a fresh primary key from the
schema's own portable uuidv7 function — not `pg_catalog.uuidv7()`, which the migration
only uses when the server has it — so pk order stays chronological on any server the
migration accepts. `ANALYZE` both queue tables afterwards: a bulk-loaded table with
stale statistics gives the planner a row count orders out, and every plan taken on it is
a different plan.

- [ ] **Step 6: Run the tests**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_seed.py -v` Expected: PASS.

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: seed the queue tables by cloning drained templates"
```

---

### Task 5: One deterministic test for the suspension question

**Files:**

- Test: `tests/core/test_durable.py`

**Interfaces:**

- Consumes: the durable-sleep task and worker helpers `tests/core/test_durable.py`
  already uses. Read that file and reuse its fixtures rather than adding new ones.

**Why a test and not a stage.** A run's own state cannot witness the failure: the SDK's
sleep sets `state = 'sleeping', claimed_by = null, claim_expires_at = null` before
`SuspendTask` propagates (`django_absurd/migrations/0001_initial_0_5_0.sql:1160-1168`),
so a parked thread leaves exactly the row a released one does. Counting sleeper runs by
state would report a tautology as corroboration.

`tests/core/test_durable.py` already drives a durable sleep through the real sync
bridge, and `tests/core/test_worker_run_refill.py` covers a worker claiming while a slot
is busy. The uncovered case is N sleepers against C slots with a quick task behind them.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.django_db(transaction=True)
def test_a_quick_task_runs_while_more_sleepers_than_slots_are_suspended():
    """A suspended task occupies no worker slot, so a quick task queued behind
    two of them still runs on a single-slot worker.

    Asserted on the quick task reaching its completed state: if a durable
    sleep parked its pool thread, one slot would be held by the first sleeper
    and the quick task would never be claimed. Two sleepers against one slot
    so the case is not merely "a slot happened to be free".
    """
    for _ in range(2):
        tasks.sleep_for_a_while.enqueue(seconds=30.0)
    quick = tasks.noop.enqueue()

    with running_worker(concurrency=1):
        wait_for_task(quick, "completed", timeout_s=30.0)

    assert read_task_state(quick) == "completed"
```

Names for the sleeper task, `running_worker`, `wait_for_task` and `read_task_state` come
from what `tests/core/test_durable.py` and `tests/utils.py` already provide. Read them
and use the real names; do not add parallel helpers.

- [ ] **Step 2: Run it**

Run:
`PGPORT=5452 uv run pytest tests/core/test_durable.py -k more_sleepers_than_slots -v`
Expected: PASS — the behaviour is believed correct. This test documents and guards it.

- [ ] **Step 3: Prove it can fail**

Make the sleep block the thread instead of suspending — the crudest version is a
`time.sleep` in the task body — and confirm the test fails by timeout. Revert. Record it
in the commit body. Without this the test is decoration.

- [ ] **Step 4: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: a quick task runs past more sleepers than slots"
```

---

### Task 6: Make `bench_harness` a required check

**Files:**

- Modify: GitHub ruleset `18038740` (no repo files)

- [ ] **Step 1: Confirm the harness branch is merged**

Requiring this check while Task 2 is in review would block the PR that fixes the flake.

- [ ] **Step 2: Read the current required-check list**

```bash
gh api repos/lincolnloop/django-absurd/rulesets/18038740 \
  -q '.rules[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
```

Fifteen contexts today, including `dev`.

- [ ] **Step 3: PATCH with the existing list plus `bench_harness`**

Read the response back and diff it against Step 2's output plus the one addition. A
dropped entry here silently removes a merge gate.

- [ ] **Step 4: Verify on a live PR**

Confirm `bench_harness` shows as required before considering this done.

---

### Task 7: Take the run

**Files:**

- Create: a dated results directory (git-ignored)

Not a coding task. A procedure whose output is the evidence for Task 8.

- [ ] **Step 1: Confirm the tree**

Tasks 1–5 merged. `tox -e dev,bench_harness` green.

- [ ] **Step 2: Start the tuned server and migrate it**

```bash
PGPORT_BENCH=5460 docker compose --profile bench up -d --wait db_bench
```

Then run the migration step `benchmarks/README.md` documents. `db_bench` is a RAM data
directory, so it comes up empty every time and a run against an unmigrated server fails
at the first enqueue.

- [ ] **Step 3: Quiet the machine and hold it awake**

Mains, not battery. Prefix with `caffeinate -is`. This machine has slept 25–284 s
mid-run before; `perf_counter` does not count the nap, so a napped arm reads fast and
entirely plausible.

- [ ] **Step 4: Run every stage at the default durable duration**

Leave `--durable-seconds` at its 2 s default. The durable body is dominated by its
sleep, so 30 s buys ~48 minutes of wall clock and no additional signal — its arms exist
to exercise the connection budget, which the backend probe samples while bodies run.

Expect roughly 45 minutes. Do not touch the machine.

- [ ] **Step 5: Stamp what the harness does not stamp itself**

`benchmarks/host.py` already records the git SHA, core count, `shared_buffers` and
cluster name. Add beside the results the power source and anything else running —
neither is recoverable later, and both change what the numbers mean.

- [ ] **Step 6: Check the run before trusting it**

Per arm: cv, whether every rep drained, whether any rep was invalidated for extra runs.
An arm with wide cv is reported as wide. If the commit-ceiling probe refused, that arm's
connection-bound verdict is unavailable rather than guessed.

---

### Task 8: Restate the findings from that run

**Files:**

- Modify: `benchmarks/CLAUDE.md`, `benchmarks/README.md`

Own branch, off `main` after the harness branch merges and the run is done.

- [ ] **Step 1: Write the `checkpoint_cost` finding, which does not exist**

`benchmarks/CLAUDE.md` says itself that this stage has no finding. It is the stage whose
shape most resembles durable agent work — a `ctx.step` per tool call. State what the run
measured and what it means for a workflow checkpointing per step.

- [ ] **Step 2: Replace multi-process figures only**

Every multi-process rate measured before the windowing and spawn fixes is not comparable
with one measured after; replace those. **Keep the single-process ranges** — the spawn
bias is absent at one process, so the overhead itemisation, the concurrency ladder and
the statement-level costs remain valid, and the new run is ADDED to their range rather
than replacing it. Trading four runs of evidence for one is a loss, not an update.

- [ ] **Step 3: Carry cv and the noise floor on every restated number**

`benchmarks/CLAUDE.md` establishes median between-run cv 4.7%, worst 12.5%, and tells
the reader to treat a difference under ~12% as noise. A one-sample run supports a rank
order or a ratio above that floor and not a point estimate. No restated figure may read
as one.

- [ ] **Step 4: Answer the remaining questions in prose**

Whether the 2.45x is table size or queue depth; what the connection budget does when
bodies hold their threads. Each with the number, the method, and what it does not
support.

- [ ] **Step 5: Delete only the defect notes the run proves gone**

The `commits_per_task` / `calls_per_task` note goes if and only if the windowed run
shows the ratio holding above one process. The `process_scaling` ladder confound STAYS —
it is still real and the report still prints `CONFOUNDED:`.

- [ ] **Step 6: Name the run on every finding**

A figure with no run is a defect a reader can catch by reading for it.

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "docs: restate the harness findings from a windowed run"
```

---

### Task 9: The admin-at-volume stack

**Files:**

- Create: `benchmarks/admin_at_volume/__init__.py`,
  `benchmarks/admin_at_volume/README.md`
- Modify: `benchmarks/settings.py`
- Test: `tests/benchmarks/test_admin_at_volume.py`

Own branch, off `main` after `fix__admin-changelist-order-by-pk` merges — the probe's
arms read changelists ordered by pk, which is that branch's change.

**No new Postgres service.** Only `db_bench` is tmpfs; the plain `db` service the suites
already use is a real data directory. The corpus is its own DATABASE on that server.

**No settings module of its own.** Mount the admin and its dependencies in the harness's
existing `benchmarks/settings.py`, behind the same env-var switch that selects the
corpus database. A separate settings module no test imports lands at 0% coverage under
`[tool.coverage.run] source` and turns the patch status red — and a test that runs under
`tests.benchmarks.settings` would prove that module, not this one.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.django_db(transaction=True)
def test_the_task_changelist_pages_a_seeded_corpus(admin_client):
    """The changelist's own result count is what a person sees paging it.

    Asserted against the number seeded, so a changelist rendering an empty
    table cannot pass, and so can a filtered queryset that silently drops
    rows.
    """
    seed.seed_queue_tables(rows=200, queue="bench")

    response = admin_client.get(reverse("admin:django_absurd_task_changelist"))

    assert (response.status_code, response.context_data["cl"].result_count) == (200, 200)
```

The second element is the seeded count; the first is the status. Fix the tuple to
`(200, 200)` only if the status code and the row count genuinely coincide — otherwise
write them as a dict of two named keys, which reads better and cannot be confused.

- [ ] **Step 2: Run it and watch it fail**

Run: `PGPORT=5452 uv run pytest tests/benchmarks/test_admin_at_volume.py -v` Expected:
FAIL — the admin is not installed in the harness settings.

- [ ] **Step 3: Install the admin in the harness settings**

Add the admin app and its dependencies — sessions, messages, auth, staticfiles, a
templates entry — to `benchmarks/settings.py`, and a URLconf mounting the admin. Follow
`tests/settings.py`, which already does exactly this for the core suite; copy its shape
rather than inventing one.

- [ ] **Step 4: Run the test**

Expected: PASS at 200 rows, which is what CI runs.

- [ ] **Step 5: Write the README**

For a person: start `db`, create the corpus database, seed N rows, create a superuser,
run the server, open the admin. Every command copy-pasteable. State that the corpus is
synthetic — uniform ages, a synthetic `claimed_by` spread — so nobody quotes a number
from it as a property of the library.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: serve django-absurd's admin over a seeded corpus"
```

---

### Task 10: The admin probe

**Files:**

- Create: `benchmarks/admin_at_volume/probe.py`
- Test: `tests/benchmarks/test_admin_at_volume.py`

**Interfaces:**

- Produces: `probe_admin_arms(*, results_dir)` returning `{"arms": [...]}` where each
  arm carries `name`, `cold_ms`, `warm_ms` and `queries`, and writes
  `explain_<name>.txt` into `results_dir`.

Arms: the tasks changelist first page; the runs changelist first page; and the tasks
changelist filtered by state, because a filter changes the plan and ordering by pk does
not help it.

**What this does not re-measure.** The pk-ordering fix is already measured at 3M runs in
the commit that makes it — ~520k pages and 84.7 s cold before, an `Index Scan Backward`
at 33 buffers with the same latency cold or warm after. These arms exist to find the
NEXT bottleneck.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.django_db(transaction=True)
def test_every_arm_records_a_query_count_and_keeps_its_plan(tmp_path):
    """A timing alone cannot say why a page was slow, so an arm without a plan
    is not a finding.

    Asserted on the artefacts rather than the durations, which are this
    machine's to decide. The query count is asserted against the admin's own
    floor of a count query plus a page query, so an arm that redirected to a
    login page cannot pass with a single query.
    """
    seed.seed_queue_tables(rows=200, queue="bench")

    summary = probe.probe_admin_arms(results_dir=tmp_path)

    arms = {arm["name"]: arm for arm in summary["arms"]}
    assert set(arms) == {"tasks_first_page", "tasks_filtered_by_state", "runs_first_page"}
    assert {
        name: (arm["queries"] >= 2, (tmp_path / f"explain_{name}.txt").exists())
        for name, arm in arms.items()
    } == {name: (True, True) for name in arms}
```

- [ ] **Step 2: Run it and watch it fail**

Expected: FAIL — no `probe.py`.

- [ ] **Step 3: Implement the probe**

Per arm: issue the request twice, keeping the first as cold and the second as warm;
capture the queries the request ran; and dump the changelist query's plan with `ANALYZE`
and `BUFFERS` to a per-arm file. Write one JSON summary beside them.

Time a real request through the stack rather than the ORM alone — rendering 100 rows is
part of what a person waits for.

- [ ] **Step 4: Run the test**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add -A && uv run pre-commit run --all-files
/usr/bin/git commit -m "test: time the admin changelists and keep their plans"
```

---

## Self-review

**Spec coverage.** Multi-process figures replaced → Tasks 3, 7, 8. cv and noise floor →
Task 8 Step 3. `checkpoint_cost` finding → Task 8 Step 1. Four questions → Tasks 3,
7, 8. Seeder + drift guard + drain → Task 4. Suspension settled → Task 5. Admin
clickable + README → Task 9. Admin timings + `EXPLAIN` → Task 10. Required check →
Task 6. Branch topology → the preambles of Tasks 8 and 9.

**Deliberate absences.** Retry storms, cleanup keep-up, suite speed, the `worker_knobs`
durable arm and multi-queue routing are deferred in the spec with reasons. No tasks.

**Type consistency.** `seed.seed_queue_tables` / `seed.check_queue_table_shape` /
`seed.QueueTableShapeError` consistent across Tasks 4, 9, 10. `count_rows` defined once
in Task 4 as a `tests/benchmarks/utils.py` helper.
`probe.probe_admin_arms(*, results_dir)` matches its call in Task 10's test. Task 5
names no new helper — it reuses what `tests/core` already has, and says to read for the
real names.

**Where an implementer must stop rather than guess.** Task 3 Step 2, if the stagger
cannot be observed at an affordable N. Task 2 Step 1, if either diagnosis is wrong. Task
9 Step 1, on the assertion's shape.
