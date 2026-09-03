# Truthful harness, and the admin at volume — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-task counts exact above one worker process, a written `checkpoint_cost`
finding, and a seeder that puts millions of rows in front of the admin.

**Architecture:** One branch. Land what exists, fix the counting window, add the seeder,
take one ~20-minute run over three stages, restate the docs from it.

**Spec:** `docs/specs/2026-09-03-truthful-harness-and-admin-at-volume.md`

## Global constraints

Every task inherits these.

- `import typing as t` always. Absolute imports only. Functions contain a verb. Helpers
  go BELOW their callers. No leading-underscore module constants.
- Comments answer "why this, not the obvious alternative", ≤2 lines. Docs and docstrings
  state what IS — no "previously"/"now"/before-after framing.
- Function-based pytest tests only. Assert positive observables, never absence. Assert
  the SHAPE of a measurement, never a rate — the rate is the machine's to decide.
- **Never** add a ruff ignore, `noqa`, or `# pragma: no cover`; **never** monkeypatch
  (`tests/CLAUDE.md` allows one carve-out and this is not it); **never** unit-test an
  internal helper. Fix the code or stop and ask.
- Any test whose worker children must see committed rows needs
  `pytest.mark.django_db(transaction=True)` — children cannot see an open transaction.
- **Never** add AI attribution to a commit. **Never** `git commit --amend`. **Never**
  bare `git stash` — the stack is shared across worktrees; use a WIP commit.
- `export PGPORT=5452 PGPORT_PGCRON=5453 PGPORT_BENCH=5460` on every command touching
  Postgres, `docker compose up` included. No `timeout` on this machine — poll in Python.
- Suites need `-n` explicitly: `uv run pytest tests/benchmarks -n4`.
- Gates: `uvx --with tox-uv tox -e dev,bench_harness`, then
  `/usr/bin/git add -A && uv run pre-commit run --all-files` (add BEFORE pre-commit;
  `--all-files` skips untracked). Never invoke ruff or mypy directly. Root `CLAUDE.md`
  still says `tox -e dev` runs all four suites — it runs three; do not "correct" this
  plan toward it.
- Coverage is gated at 100% merged, project + patch, over
  `source = ["benchmarks", "django_absurd", "tests"]`. Unexecuted files under those
  roots are scanned in, so a new module no test imports lands at 0% and turns the patch
  red.
- Where a step says to prove a guard bites, apply the mutation, confirm the test fails,
  revert, confirm it passes, and record the mutation in the commit body.

---

### Task 1: Land what exists, and fix the docs it falsifies

**Files:** whole tree via cherry-pick; `benchmarks/CLAUDE.md`; `benchmarks/README.md`

- [ ] **Step 1: Cherry-pick, oldest first**

```bash
/usr/bin/git cherry-pick 1286bf1   # durable workload, backend probe, concurrent spawn
/usr/bin/git cherry-pick 1c9a8dd   # claim-lease tests
/usr/bin/git cherry-pick c516581   # size_vs_depth stage
```

All three base on `86d3380`, and `7f86ef5` (PR #267) has since rewritten the same
documentation hunks — so **expect conflicts in `benchmarks/CLAUDE.md` and
`benchmarks/README.md`, and none in `stages.py`**. Take `1286bf1`'s side in both.

- [ ] **Step 2: Delete the sentences the backend probe falsifies**

PR #267 wrote that nothing measured the body-opened connection, that
`measure_shape_connections` cannot see one, and that a connection a task body opens is
invisible to the probe. `1286bf1` makes all three false — its probe samples
`pg_stat_activity` while durable work runs. Delete them and state what the probe does.

`1286bf1` also leaves `benchmarks/CLAUDE.md` saying both that the fleet starts one child
at a time and that it starts all at once. Delete the stale one. Documentation states
what IS, so leaving both is a contradiction rather than history.

Keep the `commits_per_task` / `calls_per_task` note — Task 2 fixes that defect and Task
4 removes the note once a run shows it gone.

- [ ] **Step 3: Fix the two flaky assertions in `tests/benchmarks/test_smoke.py`**

Both failed once, on CI run `33710633257`, on a markdown-only commit — environment
sensitivity, not a regression. Worth fixing because `bench_harness` becomes a required
check, not because it fires often.

`test_rate_ramp_measures_at_the_highest_offer_it_absorbed` failed on
`climbed_before_it_refused`: the ramp refused its FIRST rung, so `absorbed` was empty.
`RAMP_CEILING_PER_S = 900.0` is fixed and `RATE_RAMP_START_FRACTION` of it exceeded what
a 2-worker fleet on that runner absorbed. The test needs the ramp to absorb at least one
rung AND refuse one, and a fixed ceiling cannot put both a fast workstation and a slow
shared runner inside that window. Derive the ceiling from a short real drain the machine
performs instead, so the first rung is absorbable and the top rung is out of reach by
construction — which is what the production stage does, reading the drain ceiling off
`stage_process_scaling.json`. The sibling test above already covers refuse-everything.

`test_commit_ceiling_probe_times_a_warmed_session_not_a_cold_one` failed on
`timed_only_the_rounds_it_kept`, which checks `timed_s < 0.75 * elapsed_s`. The honest
ratio sits near 0.55 and runner noise pushed it past 0.75; a probe that summarized its
warm-up too lands near 1.0. Raise the threshold to the loosest value that still fails
that mutant and update the docstring to match.

Prove both guards still bite.

- [ ] **Step 4: Run both suites, three times for the flake fix**

```bash
PGPORT=5452 uv run pytest tests/benchmarks -n4
PGPORT=5452 uv run pytest tests/core -n4
```

- [ ] **Step 5: Commit the doc fix and the flake fix separately**

The cherry-picks are already commits. Add two more:

```bash
/usr/bin/git commit -m "docs: describe one fleet start-up and a probe that sees a body's connection"
/usr/bin/git commit -m "test: give the ramp and ceiling probes tolerances a shared runner can meet"
```

---

### Task 2: Count a rep's commits and runs over the same window

**Files:** `benchmarks/measurement.py`, `benchmarks/analysis.py`; test in
`tests/benchmarks/test_cli.py`

The defect is denominator-only. A saturation rep reads `commits_before` AFTER
`start_workers` returns, but `analyze_saturation` counts runs over the whole drain — so
the numerator excludes the start stagger and the denominator includes it, and
`commits_per_task` reads low above one process.

**Do not reuse the existing `since` mark.** It filters `t.enqueue_at`, and a saturation
rep enqueues everything BEFORE the fleet starts, so a post-fleet mark selects zero
tasks: `n_runs` 0, a degenerate window, and every multi-process number the run exists to
produce destroyed. The new window must filter on **`r.completed_at`**, which is what the
commit counter's own start time corresponds to.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.django_db(transaction=True)
def test_a_saturation_rep_counts_runs_over_the_window_its_commits_came_from(tmp_path):
    """The commit counter starts once the fleet is up, so the run counter must too.

    Asserted against a count taken independently from the same mark rather than
    against a ratio: a ratio would encode this machine's speed, while the two
    counts must agree on any machine or the metric divides one window by another.
    """
    stages.main(["process_scaling", "--results-dir", str(tmp_path), "--tasks", "400"])

    rep = json.loads((tmp_path / "stage_process_scaling.json").read_text())
    two = next(m for m in rep["measurements"] if m["processes"] == 2)

    assert {
        "counted_runs": two["n_runs"],
        "runs_completed_after_the_mark": count_runs_completed_after(two["window_start"]),
    } == {
        "counted_runs": two["n_runs"],
        "runs_completed_after_the_mark": two["n_runs"],
    }
```

`count_runs_completed_after` is a new helper in `tests/benchmarks/utils.py`, beside the
counting helpers already there. The rep must record its `window_start` for this to be
checkable at all — that recording is part of the implementation.

- [ ] **Step 2: Run it and watch it fail**

```
PGPORT=5452 uv run pytest tests/benchmarks/test_cli.py -k counts_runs_over_the_window -v
```

Expected: FAIL — the rep records no `window_start`.

- [ ] **Step 3: Window the per-task counts on completion time**

Capture a database mark beside `commits_before`, record it on the rep, and give the
metrics that divide by task count a predicate on `r.completed_at` greater than that
mark. Leave the throughput profile alone: it is already p10-p90 trimmed on
`r.completed_at` and absorbs the stagger by construction.

- [ ] **Step 4: Run it and watch it pass, then run the whole suite**

`size_vs_depth` passes its own `enqueue_at` mark for a different purpose; confirm its
tests still hold.

- [ ] **Step 5: Prove the guard bites**

Move the mark to before `start_workers` and confirm the test fails.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git commit -m "test: count a rep's runs over the window its commits came from"
```

---

### Task 3: The clone seeder, its drift guard, and how to browse the corpus

**Files:** create `benchmarks/seed.py`; test `tests/benchmarks/test_seed.py`; document
in `benchmarks/README.md`

**Interface:** `seed_queue_tables(rows, *, queue)` returns a summary carrying `tasks`,
`runs` and `elapsed_s`, counted from the tables themselves; `check_queue_table_shape()`
raises `QueueTableShapeError` naming every column it expects and cannot find.

- [ ] **Step 1: Write the drift guard's failing test**

```python
@pytest.mark.django_db(transaction=True)
def test_seeding_refuses_a_queue_table_whose_shape_it_does_not_know(_isolate_queues):
    """The guard fails the seed, not the read.

    A clone that writes a table it half-understands produces rows that look right
    and are not, so the only safe failure is before any row is written. Driven by
    really altering the table rather than by patching what the seeder believes,
    and the error names the column so the next reader learns which upstream change
    moved.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("alter table absurd.t_bench drop column params cascade")

    with pytest.raises(seed.QueueTableShapeError) as caught:
        seed.seed_queue_tables(rows=10, queue="bench")

    assert "params" in str(caught.value)
```

`cascade` is required: `params` is in the tasks `EntitySpec`, and `django_absurd` builds
a `CREATE VIEW … UNION ALL` over every queue's task table from that spec on
`post_migrate`, so the column has a dependent view in every test database. Without
`cascade` the statement raises `DependentObjectsStillExist` and the RED step fails for
the wrong reason. The `_isolate_queues` fixture hard-drops and re-provisions the schema,
restoring the view.

- [ ] **Step 2: Run it and watch it fail**

Expected: FAIL — no `benchmarks/seed.py`.

- [ ] **Step 3: Write the guard**

Read the live columns for each queue table from `information_schema`, compare against
what the clone writes, and raise a typed error naming every column expected and absent.
The error owns its message; the caller assembles no text. Call it before writing
anything.

- [ ] **Step 4: Write the seeding test**

```python
@pytest.mark.django_db(transaction=True)
def test_seeding_writes_the_rows_it_reports_and_the_admin_can_page_them(admin_client):
    """Counted through the admin's own changelist, which is what a person pages.

    A seeder returning its intended count reports success for a clone that wrote
    nothing, and a changelist count proves the rows are reachable through the
    queryset the admin actually builds. Runs are asserted separately because
    enqueueing alone produces none, and a corpus with an empty runs table cannot
    answer a question about the runs changelist.
    """
    seed.seed_queue_tables(rows=200, queue="bench")

    response = admin_client.get(reverse("admin:django_absurd_task_changelist"))

    assert {
        "status": response.status_code,
        "rows_the_changelist_found": response.context_data["cl"].result_count,
        "runs_exist": count_rows("r_bench") > 0,
    } == {"status": 200, "rows_the_changelist_found": 200, "runs_exist": True}
```

This needs no settings work: `tests/settings.py` installs `django.contrib.admin` and
`tests/urls.py` mounts it, and `tests/benchmarks/settings.py` inherits both.

- [ ] **Step 5: Implement seeding**

Write a handful of template rows through the real enqueue API so their shape is whatever
the library actually writes. Drain them with a worker so finished runs exist, including
at least one failing template so failed and retried states appear. Then clone
server-side with `generate_series`, giving each clone a fresh key from the schema's own
portable uuidv7 function — not `pg_catalog.uuidv7()`, which the migration uses only
where the server has it — so pk order stays chronological anywhere the migration runs.
`ANALYZE` both queue tables afterwards: a bulk-loaded table with stale statistics gives
the planner a row count orders out, and every plan taken on it is a different plan.

- [ ] **Step 6: Document browsing the corpus in `benchmarks/README.md`**

A short section, for a person, every command copy-pasteable: point `PGDATABASE` at a
corpus database on the existing `db` service, `migrate`, seed N rows, `createsuperuser`,
`runserver`, open the admin. `DJANGO_SETTINGS_MODULE=tests.settings` carries the admin
already. State that the corpus is synthetic — uniform ages, a synthetic `claimed_by`
spread — so nobody quotes a number from it as a property of the library.

- [ ] **Step 7: Run the tests and commit**

```bash
/usr/bin/git commit -m "test: seed the queue tables by cloning drained templates"
```

---

### Task 4: One targeted run, then restate

**Files:** `benchmarks/CLAUDE.md`, `benchmarks/README.md`

Runs from this branch. A merged tree buys nothing: findings are named by run label, and
a squash merge makes the branch SHA unreachable anyway.

- [ ] **Step 1: Start the tuned server and migrate it**

```bash
PGPORT_BENCH=5460 docker compose --profile bench up -d --wait db_bench
```

Then the migration step `benchmarks/README.md` documents. `db_bench` is a RAM data
directory, so it comes up empty every time and an unmigrated run fails at the first
enqueue.

- [ ] **Step 2: Quiet the machine and hold it awake**

Mains, not battery. Prefix with `caffeinate -is`. This machine has slept 25-284 s
mid-run before; `perf_counter` does not count the nap, so a napped arm reads fast and
entirely plausible.

- [ ] **Step 3: Run three stages, not eight**

```bash
caffeinate -is uv run python -m stages process_scaling pooled_vs_split checkpoint_cost \
  --results-dir results/<dated>
```

Roughly 20 minutes. `latency_under_load` is excluded because its reps are already
windowed on the database clock, so the spawn bias never touched them. `worker_knobs`,
`poll_interval`, `sync_vs_async` and `producer_ceiling` are excluded because their
published figures are single-process, which the spawn bias does not reach.
`size_vs_depth` is excluded until someone is about to make a retention decision.

`process_scaling` seeds the ladder `pooled_vs_split` calibrates on, so it runs first.

- [ ] **Step 4: Check the run before trusting it**

Per arm: cv, whether every rep drained, whether any rep was invalidated for extra runs.
Record the power source and anything else running beside the results —
`benchmarks/host.py` stamps the git SHA, core count and server settings, but not those,
and neither is recoverable later.

- [ ] **Step 5: Restate the findings**

Write the `checkpoint_cost` finding, which does not exist — `benchmarks/CLAUDE.md` says
so itself. It is the stage whose shape most resembles durable agent work.

Replace multi-process figures with the windowed ones. **Keep every single-process
range** and add this run to it: the spawn bias is absent at one process, so the overhead
itemisation, the concurrency ladder and the statement-level costs stand, and replacing
four runs of evidence with one sample is a loss.

Carry cv and the ~12% noise floor on every restated number. One sample supports a rank
order or a ratio above that floor, never a point estimate.

Restate the connection budget from the landed measurement — `C + 2` per process once a
body touches the ORM — rather than from this run.

Delete the `commits_per_task` / `calls_per_task` note if and only if the windowed run
shows the ratio holding above one process. Keep the `process_scaling` ladder confound:
it is still real and the report still prints `CONFOUNDED:`.

Say which stages this run did not touch, so a reader knows which figures come from
earlier runs.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git commit -m "docs: restate the harness findings from a windowed run"
```

---

## After merge

One line for the PR description, not a task: add `bench_harness` to the required status
checks on ruleset `18038740`, which carries 15 contexts today including `dev`.
