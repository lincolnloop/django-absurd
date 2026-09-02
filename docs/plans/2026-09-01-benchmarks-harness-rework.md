# Plan: benchmarks harness rework

Spec:
[2026-09-01-benchmarks-harness-rework.md](../specs/2026-09-01-benchmarks-harness-rework.md)
PR: https://github.com/lincolnloop/django-absurd/pull/257

Three phases. Claims deleted in 1, earned back in 3.

## Branch

Stacked, not a takeover.

- Branch off `origin/benchmarks__load-test-harness` HEAD. **Do not rebase onto `main`**
  — that moves the merge-base off its HEAD and drags main's commits into the stacked
  diff. The base branch resolves its own lag at merge time.
- PR targets `benchmarks__load-test-harness`. #257 stays the merge vehicle.
- GATE: the PR author is told out of band before the first push. Nothing pushed until
  then.

---

# Phase 1 — land #257 with structure fixed and claims removed

Large. Splits cleanly into two stacked commits-or-PRs if the review gets unreadable:
packaging and CI (1.1, 1.8) separate from behaviour (1.2-1.7).

## 1.1 Standalone project

Mirror `examples/web`. Working directory `benchmarks/`.

- `pyproject.toml` — own project, declares the `benchmarks` package, django-absurd via
  `[tool.uv.sources] path = "../.."`, `dj-database-url` pinned, own coverage config
  including **subprocess coverage** — task bodies run only in worker children, so
  without it they read zero.
- `uv.lock` — committed.
- `Dockerfile` — pinned Python patch version, pinned uv image, `uv sync --locked`, build
  context repo root, `SETUPTOOLS_SCM_PRETEND_VERSION`, uv cache mount.
- `compose.yaml` — database pinned to an exact patch tag, tuned server config and named
  volume kept, **no `ports:`**. Runner service builds the Dockerfile, no inline `sh -c`
  chain. One-shot migrate service gating the rest. Keep the profile gate on the runner:
  it is what stops a plain `up -d` starting a long run.
- `settings.py` — `DATABASE_URL` parsed by `dj-database-url`, defaulting to the compose
  service name. Test database name applied after the parse.
- **`runner.py` — moves with it.** It builds worker children's environment from the
  connection's settings dict using `PG*` names. Left alone while settings read only
  `DATABASE_URL`, children ignore that environment and connect to the **persistent**
  database instead of the test one: the suite then hangs to timeout, or drains real
  data. This is the mechanism the README credits for letting tests run against a
  throwaway database, so it is not incidental.
- Root `pyproject.toml` — coverage source gains `benchmarks`, so the harness sits inside
  the repo-wide gate rather than a flag of its own, and the root addopts ignore the new
  suite alongside the other three.
- `renovate.json` — add the benchmarks project to `postUpgradeTasks` commands and its
  lockfile to `fileFilters`. Without it every root bump stales the lock and
  `uv sync --locked` fails the build.

**Reversed after landing, with the container-only decision — see the spec.** Python runs
on the host under uv from inside `benchmarks/`, so the `Dockerfile`, the runner service
and the one-shot migrate service are gone, and with them the CI job that built the
image. `pyproject.toml` and `uv.lock` stay and are what `uv run` resolves. The database
keeps its exact tag, tuned config and named volume, gains `ports:` because the driver
now reaches it over loopback, and moves into the ROOT `compose.yaml` beside `db` and
`db_pg_cron` — one file for every service in the repo. The profile gate survives the
move, on the database rather than the runner, for the same reason it was there: a bare
`up -d` brings up the suites' servers and nothing a benchmark needs. `settings.py`
defaults to `localhost:${PGPORT_BENCH:-5460}` instead of a compose service name. The
`renovate.json` bullet outlives its own reason: with no `uv sync --locked` to fail a
build, re-locking in the same branch is the only thing left stopping a stale lock, which
now resolves silently on the host.

## 1.2 Stage rename

Letters out, descriptive names in, dependency graph explicit as a module-level mapping.
Default order topological.

Touchpoints. The first list of these was itself incomplete, which is the failure mode:

- stage name tuple, descriptions, per-stage run functions, per-stage build functions
- calibration reads and the results filename writer
- **report dispatch on letters** — the report branches on stage identity to pick which
  table and which derived analysis to render. Missed, the producer stage renders under
  throughput columns with fabricated values and three stages lose their analyses, with
  no error raised.
- **measurement names embed letters** (`a1_c*`, `a2_batch_*`, `b_workers_*`, `c_poll_*`,
  `e_flat`, `f_*`, `g_rate_*pct`), written into results files and shown in report rows.
  Untouched, a renamed harness still speaks letters at the reader.
- README: stage table, regression-diff note, cold-restart section, the
  `--stage A ... A to G` usage, the architecture diagram, the layout table.

Missing-prerequisite error names the stage it needs.

## 1.3 CLI

Positional stage names, `nargs="*"`. Empty runs all, topologically ordered. A named
stage with a missing prerequisite still errors rather than running it — ordering governs
the run-all case only, and the help says so.

Direct size flags, as needed:

- `--tasks` — saturation stages
- `--duration` — rate stages, **including the idle probes**, whose seconds are currently
  a hardcoded constant governed by neither flag and would otherwise dominate a smoke run

No `--cold` flag — see the spec, where the reasoning was reversed and the outcome was
not. Delete the shell script; document the two-command restart sequence per stage in the
README instead.

## 1.4 Bug fixes — RED first

**Single rep reads as perfectly stable.**

- RED: run a measurement with one rep, assert spread is `None` and the measurement is
  flagged. Then the same through the producer stage, which has its **own** summariser
  with the same defect — fixing one site only is the half-fix this plan keeps warning
  about.
- Currently spread is `0.0` and flagged is false, so a dry run reports as the most
  stable measurement in its stage. Verified live from a committed results file.
- The existing smoke test asserts the unflagged result, so it depends on the bug and
  changes with the fix.
- Fix: too few reps to have a spread takes the same branch as no positive median. Check
  calibration still falls back correctly when every candidate is flagged.

**Saturation rows publish latency percentiles.**

- RED: render a saturation results file, assert the end-to-end percentile columns are
  absent — and assert the same for the **console** summary line, which prints them too.
- Fix: suppress for saturation rows in both places.

**Prerequisite errors surface as tracebacks.**

- RED: invoke a stage whose prerequisite file is absent; assert a clean message,
  non-zero exit, and that **no subsequent stage runs**. The failing stage's own header
  printing first is current behaviour and not the defect.
- Cover the uncalibratable case too, not only the missing-file one. Provoke it with a
  ONE-task run: a single completion puts p10 and p90 at the same instant, so the window
  is empty by construction whatever the worker config. Four tasks does not do it — the
  low-concurrency rungs of the ladder still measure, so the stage calibrates and the
  error never fires. That premise was wrong in an earlier draft of this plan.
- Fix: catch both at the CLI boundary.

## 1.5 Claim deletions

From `benchmarks/README.md` and, where they appear, the docs page being deleted:

- the concurrency / core-count rule, and the ladder-not-scaled rationale resting on it
- the "largest single improvement anywhere" ranking language. Absolute rates with host
  context stay. Do not replace it with a ratio against the flagged baseline.
- "only the ratios travel" where stated generally

Not a deletion in the harness README, which keeps the number under the provenance stamp.
It does NOT go in the user guide: the same branch marks that whole regime as unbacked by
anything in the repo, so citing one of its numbers as guidance two files away
contradicts the stamp. The guide gets the mechanism — a run still pays for its own claim
and completion, which do not overlap away — and phase 3 can put a number back with
evidence behind it.

## 1.6 Docs

- Delete `docs/web/performance.md`, its nav entry in `zensical.toml`, and the bullet in
  `docs/web/workers.md` pointing at it.
- **Before deleting, rehome the supported findings.** The batch-size floor and the
  poll-interval latency floor go to the worker docs as terse flag guidance in the
  pattern _default, then why you would change it_ — the flags table already carries the
  defaults, so add only what it does not say. The IO-bound concurrency ladder does NOT
  go with them; see 1.5. Two are not worker flags: the checkpoint cost goes to
  `workflows.md` beside `step`, and the enqueue-batching pattern to `tasks.md` beside
  the rule that enqueuing rides the surrounding transaction, since it is a producer
  pattern. Nothing is rehomed without its host context, and the batching pattern keeps
  no ratio — the 12x divides by the baseline that failed to replicate.
- Delete `.envrc.example` and revert the `CLAUDE.md` block instructing devs to copy it.
  No local tool imposed. The two operational facts the file carried survive as prose in
  `CLAUDE.md`'s Testing section, where a developer meets them: what an unset `PGPORT`
  costs, and why the xdist worker count wants pinning.
- Correct the `CLAUDE.md` claim about bare root `pytest`: exit code 4, an
  `ImproperlyConfigured` raised while importing auth models from the root test conftest.

## 1.7 Tests — a fourth suite, entrypoint only, happy path first

The suite lives at `tests/benchmarks`, beside the other three, not inside the harness.
Nothing in the harness imports its tests, and the suite measures nothing — it asserts
files written, counts and flags, never a rate — so the tuned benchmark server buys it
nothing and it runs against the parent compose database like every other suite.
Confirmed by running it with that server stopped.

Same shape as `tests/multidb`: own settings inheriting `tests.settings`, own
`pytest.toml` with `--confcutdir=..` and `--cov-append`, one more line in both tox envs.
Worker children still reach the right database — `build_worker_env` hands them the
parent's connection settings, so they load the harness settings module while pointing at
the suite's test database.

That leaves the benchmark server with exactly one purpose, real runs, instead of two.

Replace the existing tests. Delete unit tests on internal helpers.

Through the entrypoint, at small sizes:

- each stage runs and writes its results file
- missing and uncalibratable prerequisites refuse cleanly, and stop the run
- bare invocation runs prerequisites before dependents
- the report renders a results directory
- the two flagging fixes from 1.4, at both their sites

Assert the entrypoint ran, wrote, rendered. **Never assert a measurement came back
unflagged** — at ten tasks the degenerate-window and spread checks trip routinely, and
that is correct behaviour.

Then triage what the happy path missed. Known candidates, all currently covered by unit
tests being deleted: worker-readiness timeout, crashed-worker reporting, the
stop-workers kill path, start-up cleanup, the drain deadline, the suspension guard, the
redelivery flag arm, and the git-sha fallback whose two branches cannot both be reached
in one environment. For each: reachable through the entrypoint with more effort, dead
and deleted, or a contract to assert directly. Decide per branch, with the code in front
of you — not in advance.

## 1.8 CI

Nothing new. The suite is a tox line in both envs, CI already runs tox with the parent
database up, and `--cov-append` folds it into the merged coverage number. No separate
job, no separate flag, no container coverage-path rewrite — all of which the earlier
draft specified because it assumed the tests needed the benchmark server.

## 1.9 Sweep: every test through the command line — DONE

Outcome: 38 tests. Two files enter below `stages.main` / `report.main`, each recording
why in place rather than here: all five in `test_runner.py`, which cover what happens
when a worker child misbehaves, and five of the seven in `test_smoke.py`, which build a
`MeasurementSpec` and call `run_measurement` directly — three want a task that
misbehaves and every workload the driver offers succeeds, two want one measurement
repeated and the smallest stage would charge six of them.

The premise below was wrong in one respect worth keeping: the guards were called
unreachable, and all of them turned out reachable — a child that traps its stop signal,
one that talks past its deadline, one that refuses an undeclared queue, a backlog that
outlives its drain deadline, and a thread that moves the wall clock while the monotonic
one stays put. `benchmarks/` is at 100% statement and branch. One guard was deleted
rather than covered, because the only input that reached it turned nonsense into a
plausible number.

Superseded, kept for the reasoning:

## 1.9 (original) Sweep: every test through the command line

After 1.1 and 1.4 land. Enumerate every call in `tests/benchmarks` that reaches below
`stages.main` / `report.main` and resolve each one. Four are known to remain, each with
a recorded reason:

- **redelivery** — needs a claim-timeout AND a max-attempts flag, because the workloads
  carry no retry params and would end failed rather than redelivered. Two knobs whose
  only use is breaking the harness.
- **never-completes** — no workload fails. The only CLI route trips CPython's own sleep
  validation, which is coverage resting on an accident.
- **worker readiness** — reachable honestly: a queue that is not declared makes the
  worker exit before its readiness line. Needs the producer to enqueue with an explicit
  queue rather than through the task object, which is a real capability the harness
  lacks. **Take this one.**
- **crashed worker** — needs a worker dying after readiness. No input produces it.

For each survivor: route it through the CLI, delete it as covered elsewhere, or record
it as a documented exception. Do NOT delete a guard to make the sweep finish — they are
load-bearing for a 75-minute run, which is the run nobody is watching.

## Sequencing note

The rename (1.2) breaks the existing suite that 1.7 replaces. Either do 1.7 first
against the old names, or accept one intermediate commit with a red suite and say so in
its message.

---

# Phase 2 — consolidate

Own PR, after #257 merges.

1. Port the admin probe, its seeder, the schema drift guard, the results conventions,
   the two-clock guard and the migration-state refusal onto the measurement core. The
   `loadtest/` branch is already pushed, so the archive SHA is durable.
2. The seeder's template tasks stop writing rows, so the **execution-log** model and its
   migration go. Verified: the seeder references it at exactly one call site, a delete,
   and the admin probe never touches it. The **occupancy** model stays — the
   pooled/split arms read its intervals.
3. Rename the task whose name says it burns CPU and which performs one database insert.
   It belongs here, with the body rewrite, not in phase 3.
4. Second database service and volume for the seeded dataset, in the root `compose.yaml`
   beside `db_bench`.
5. Admin probe is its own entrypoint, not a stage.
6. Record the retired spec and plan in `docs/HISTORY.md` against the pushed SHA, then
   delete the rest. Findings that already paid out belong in `docs/WHY.md` if not
   already captured.

Merge conflict with the base branch: one line in the root pytest addopts. Trivial.

---

# Phase 3 — earn the claims back

New stage `concurrency_vs_processes`: pooled (1 worker × concurrency C) against split (C
workers × concurrency 1) **at equal total slots**, across three workload classes, at two
or more values of C.

Workloads. Two do not exist in either harness and must be built:

1. wait-bound — exists
2. pure-Python compute — tight loop, no IO, no C extension
3. compute that leaves Python — a hashing or compression call over a buffer

## Pre-registration

Fixed before running.

**Equivalence criterion.** Arms count as equivalent when they differ by less than a
pre-declared minimum effect, **and both arms are unflagged**. Not "less than the
measured spread" — that rewards noise, since the noisier the run the easier equivalence
is declared, and the equivalence cells carry the boldest conclusions. A flagged arm
supports no conclusion at all.

| workload               | pooled ≈ split                                                                  | split ≫ pooled                                                              | pooled ≫ split                              |
| ---------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------- |
| wait-bound             | concurrency substitutes for processes on waiting work                           | something limits the single worker — event loop, thread pool, or claim rate | claim amortization, see below               |
| pure-Python compute    | the GIL account is wrong; the deleted claim was wrong in the opposite direction | add processes for compute, concurrency will not help                        | claim amortization, or per-process overhead |
| compute leaving Python | concurrency does parallelise work that leaves Python                            | that claim is wrong; delete it, no replacement                              | as above                                    |

Also unnamed by an earlier draft and required: what it means if the two values of C
disagree, and what to do about redelivery flags on the compute arms — a long task body
at high concurrency pushes wall time toward the claim lease.

## Confounds — control or record

- **Claim amortization. The big one.** Pooled claims up to C runs in one round trip;
  split pays one claim round trip per task. The harness's own finding is that on short
  tasks the claim round trip dominates. So `pooled ≫ split` has a mundane explanation
  with no GIL content whatever. Equalising batch size across arms controls it, at the
  cost of measuring the batch-size-1 penalty instead — decide which, and state it.
- **Manipulation check.** Confirm the compute body actually dominates round-trip cost
  before concluding anything about compute. Without it phase 3 repeats the critique this
  spec levels at the concurrency ladder.
- **Connection count.** Split holds C connections; pooled holds whatever its pool opens.
  A real asymmetry, cutting the opposite way from per-process overhead.
- **Process start-up.** Record it, but do not assume it needs the ported ramp
  measurement: the benchmarks core already blocks on each child's readiness before the
  measured phase begins, and trims the distribution's tails. Check what it already
  controls before porting an instrument to control it again.

Only after measuring: write claims back, each with its numbers and host context.
