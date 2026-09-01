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

## 1.1 Standalone project

Mirror `examples/web`. Working directory `benchmarks/`.

- `pyproject.toml` — own project, declares the `benchmarks` package, django-absurd via
  `[tool.uv.sources] path = "../.."`, `dj-database-url` pinned, own
  `[tool.coverage.run] source = ["benchmarks"]`.
- `uv.lock` — committed.
- `Dockerfile` — pinned Python patch version, pinned uv image, `uv sync --locked`, build
  context repo root, `SETUPTOOLS_SCM_PRETEND_VERSION`, uv cache mount.
- `compose.yaml` — database pinned to an exact patch tag, tuned server config and named
  volume kept, **no `ports:`**. Runner service builds the Dockerfile, no inline `sh -c`
  chain. One-shot migrate service gating the rest.
- `settings.py` — `DATABASE_URL` parsed by `dj_database_url`, defaulting to the compose
  service name. Test database name applied after the parse. `PGPORT_BENCH` and the
  hand-rolled `PG*` block deleted.
- `pytest.toml` — `pythonpath = ["."]`, `testpaths = ["."]`, `--confcutdir`. Delete the
  empty `tests/conftest.py` that was standing in for it.
- Root `pyproject.toml` keeps `--ignore=benchmarks`.
- `renovate.json` — add the benchmarks project to `postUpgradeTasks` commands and its
  lockfile to `fileFilters`. Without it every root bump stales the lock and
  `uv sync --locked` fails the build.

## 1.2 Stage rename

Letters out, descriptive names in, dependency graph explicit as a module-level mapping.
Default order topological.

Touchpoints, all of them or the rename is half-done: stage name tuple, descriptions,
per-stage run functions, per-stage build functions, calibration reads, results filename
writer, report section headers, README stage table, README regression-diff note.

Missing-prerequisite error names the stage it needs.

## 1.3 CLI

Positional stage names, `nargs="*"`. Empty runs all. `--all` and `--stage` removed.

Direct size flags, as needed:

- `--tasks` — saturation stages
- `--duration` — rate stages
- `--cold` — restart the database and wait for readiness before each stage

Delete the cold-run shell script; its loop becomes driver behaviour.

## 1.4 Bug fixes — RED first

**Single rep reads as perfectly stable.**

- RED: run a measurement with one rep, assert spread is `None` and the measurement is
  flagged.
- Currently spread is `0.0` and flagged is false, so a dry run reports as the most
  stable measurement in its stage. Verified live.
- Fix: too few reps to have a spread takes the same branch as no positive median.

**Saturation rows publish latency percentiles.**

- RED: render a saturation results file, assert the end-to-end percentile columns are
  absent.
- Fix: suppress them for saturation rows; the README already says only rate mode's are
  meaningful.

**Missing-prerequisite error surfaces as a traceback.**

- RED: invoke a stage whose prerequisite file is absent, assert a clean message and a
  non-zero exit, with no traceback and no stage header printed after the error.
- Fix: catch at the CLI boundary.

## 1.5 Claim deletions

Delete from `benchmarks/README.md`, no replacement this phase:

- the concurrency / core-count rule, and the ladder-not-scaled rationale resting on it
- the 12x ranking language; absolute rates with host context may stay
- "only the ratios travel" where stated generally
- the concurrency-ladder conclusion — restate as what a no-op workload measured

## 1.6 Docs

- Delete `docs/web/performance.md` and its nav entry.
- `docs/web/workers.md` — remove the bullet pointing at it. Flag guidance stays terse
  and in the pattern _default, then why you would change it_; the flags table already
  carries the defaults, so add only what it does not say. Keep the existing upstream
  concepts link.
- Delete `.envrc.example` and revert the `CLAUDE.md` block instructing devs to copy it.
  No local tool imposed; if a note is needed it says only that the port is
  env-configurable, and it lives in the tests guide.
- Correct the `CLAUDE.md` claim about bare root `pytest` — it ImportErrors on the root
  test conftest rather than collecting nothing.

## 1.7 Tests — entrypoint only

Replace the existing suite. Delete unit tests on internal helpers (spread arithmetic,
report rendering, host context, runner internals).

Through the entrypoint, at 1-10 tasks:

- each stage runs and writes its results file
- missing prerequisite refuses cleanly
- bare invocation runs prerequisites before dependents
- the report renders a results directory
- the two flagging fixes from 1.4

Assert the entrypoint ran, wrote, rendered. **Never assert a measurement came back
unflagged** — at ten tasks the degenerate-window and spread checks trip routinely, and
that is correct behaviour.

## 1.8 CI

New job, working directory `benchmarks/`, mirroring the examples job: bring the database
up, run the suite in the container, rewrite container coverage paths to repo-relative,
upload coverage under its own flag. Register the flag.

No Python × Django matrix — dev tooling, one environment.

---

# Phase 2 — consolidate

Own PR, after #257 merges.

1. Push the existing local harness branch first, so the archive link is recoverable from
   the remote rather than a local reflog.
2. Port the admin probe, its seeder, the schema drift guard, the results conventions,
   the two-clock guard and the migration-state refusal onto the measurement core.
3. Seeder tasks write nothing, so both models and their migrations go and the harness
   stays model-free. Verified reachable: the seeder references the execution log in
   exactly one place (a truncate) and the admin probe never touches it.
4. Second database service and volume for the seeded dataset, so the stages never
   measure against a server holding it.
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

Rename the existing task whose name says it burns CPU and which in fact performs one
database insert. That name seeded the confusion.

## Pre-registration

Fixed before running. Arms count as equivalent when they differ by **less than the wider
arm's own measured spread** — a difference smaller than the noise is not a difference.

| workload               | pooled ≈ split                                                                  | split ≫ pooled                                                                                                                 | pooled ≫ split                                                   |
| ---------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| wait-bound             | concurrency substitutes for processes on waiting work                           | something limits the single worker — event loop, thread pool, or claim rate. A finding about our worker, not about concurrency | per-process overhead dominates at this size                      |
| pure-Python compute    | the GIL account is wrong; the deleted claim was wrong in the opposite direction | add processes for compute, concurrency will not help                                                                           | as above, and surprising — worth a second look before publishing |
| compute leaving Python | concurrency does parallelise work that leaves Python                            | that claim is wrong; delete it, no replacement                                                                                 | as above                                                         |

`pooled ≫ split` in any row contradicts upstream's process-scaling recommendation
directly, and is the outcome most likely to be waved away as noise if it is not named in
advance.

## Confounds — control or record

- **Process start-up.** Split spawns C interpreters against one. The ramp measurement
  from the existing harness bounds this and must be ported alongside the apparatus.
- **Claim contention.** C claim streams against one; at small task counts this can
  dominate the effect under test.
- **Connection count.** Split holds C connections; pooled holds whatever its thread pool
  opens. A real asymmetry, and it cuts the opposite way from start-up cost.

Only after measuring: write claims back, each with its numbers and host context.
