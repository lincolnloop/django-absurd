# pg_cron revdiff follow-ups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply human-review (revdiff) annotations on the pg_cron branch: rename+reshape
the projection model to explicit option columns, install the example from its pyproject,
demo both schedulers in one project, and make pg_cron tests part of the default suite.

**Architecture:** The pg_cron scheduler materialises each settings `SCHEDULE` entry into
a projection row + a `pg_cron` job whose constant command calls a search-path-safe
wrapper fn that reads the row and calls `absurd.spawn_task`. This plan renames that
model `ScheduledJob`→`ScheduledTask` and replaces its two opaque JSON blobs (`params`,
`options`) with explicit columns mirroring `AbsurdSpawnParams`/`SpawnOptions`; the
wrapper reassembles `params`/`options` jsonb from those columns **server-side, in the
body after the row is read** (injection unchanged — all data, no dynamic SQL). Source of
truth stays settings; admin stays read-only. Test/CI config folds pg_cron into the
default suite. The example grows a second beat backend (same database, distinct queue)
to demo both schedulers.

**Tech Stack:** Django 6 Tasks + AbsurdBackend, absurd_sdk, psycopg3, pg_cron ≥1.4,
pytest/pytest-django, tox-uv, Docker Compose.

## Global Constraints

- Runtime floor **Django 6.0 / Python 3.12**; **psycopg3** required.
- **Functions contain a verb**; **`import typing as t`**; **absolute imports only**;
  **no leading-underscore module consts/helpers**; **helpers BELOW their caller**.
- Django checks: `msg`=PROBLEM, `hint`=RESOLUTION, never duplicated. **Keep the single
  `absurd.*` check namespace** (no second prefix) — annotation #1.
- Tests: **pytest function-based only**; autouse `_enable_db(db)` gives DB access (no
  `@pytest.mark.django_db`); use `@pytest.mark.django_db(transaction=True)` **only** for
  commits/DDL or a deliberately-triggered `IntegrityError`; **no
  monkeypatch/unittest.mock**; drive checks/commands by running them + asserting full
  emitted text; prefer the `settings` fixture. pg_cron-needing tests use the `pg_cron`
  marker + `ensure_pg_cron`.
- **Injection stays closed**: the `cron.job` command remains constant; runtime data
  lives only in the projection row read by the wrapper.
- **Full statement+branch coverage** on added/changed lines. **No ruff ignores/noqa
  without asking.** Delete unreachable defensive code rather than adding it.
- Migration is **unreleased** on this branch — reshape the single
  `django_absurd_pg_cron.0001_initial` in place (no new migration file, no data
  migration).
- Model source of truth is **settings**; `source="admin"` is reserved and **admin stays
  read-only** (no writable admin this plan).
- **Single-absurd-DB is a hard current limit** (E004 + `resolve_absurd_database`/router
  route ONE absurd DB). The dual-scheduler demo (Task 4) therefore uses **one database,
  two backends** — not two databases.

---

### Task 1: Rename + reshape the projection model to explicit option columns

Annotations #2 (rename) + #3 (explicit columns). The model, its single migration
(fields + wrapper fn), the reconcile row-write, and every `ScheduledJob` reference are
**interdependent** — the model can't drop `params`/`options` while reconcile still
writes them, and `reconcile.py` imports the model at module load (which
`pg_cron/checks.py` → `PgCronConfig.ready()` imports), so a half-rename makes
`django.setup()` raise `ImportError` and errors the whole suite. Write the RED tests
first, then land the coupled implementation in one phase, then go GREEN.

**Files:**

- Modify: `django_absurd/pg_cron/models.py` (rename class; replace `params`/`options`
  with named columns; `db_table`→`django_absurd_scheduledtask`)
- Modify: `django_absurd/pg_cron/migrations/0001_initial.py` (`CreateModel` fields;
  `CREATE_FN` table name + body reassembly)
- Modify: `django_absurd/pg_cron/reconcile.py` (`ScheduledJob`→`ScheduledTask`
  import+usages; `update_or_create` writes named columns)
- Modify: `django_absurd/pg_cron/management/commands/absurd_sync_crons.py` (only the
  `--teardown` help text says "ScheduledJob rows"; reword to "ScheduledTask rows")
- Rename test: `tests/test_scheduledjob_model.py`→`tests/test_scheduledtask_model.py`
- Modify tests: `tests/test_orm_models.py`, `tests/test_pg_cron_sync_rows.py`,
  `tests/test_pg_cron_post_migrate.py`, `tests/test_pg_cron_teardown.py`,
  `tests/test_run_scheduled_fn.py`, `tests/test_absurd_sync_crons_command.py`

**Interfaces:**

- Produces: `django_absurd.pg_cron.models.ScheduledTask`,
  `db_table="django_absurd_scheduledtask"`, columns
  `name, source, alias, task, cron, enabled, queue, args (JSONField default=list), kwargs (JSONField default=dict), max_attempts (IntegerField null=True blank=True), retry_strategy (JSONField null=True blank=True), headers (JSONField null=True blank=True), cancellation (JSONField null=True blank=True), idempotency_key (TextField null=True blank=True), created_at, updated_at`;
  `Meta.unique_together = (("source", "alias", "name"),)`.
- Produces: wrapper `public.django_absurd_run_scheduled(source, alias, name)` — same
  signature; reassembles `params`/`options` in the body, then
  `absurd.spawn_task(queue, task, params, options)`.
- Consumes: `resolve_spawn_options(backend, schedule) -> dict` (unchanged body) with any
  of keys `max_attempts/retry_strategy/headers/cancellation/idempotency_key`;
  `get_effective_queue(schedule) -> str` (unchanged).

- [ ] **Step 1: RED — rewrite the model test (columns + unique constraint)**

Replace `tests/test_scheduledjob_model.py` with `tests/test_scheduledtask_model.py`.
Port the existing unique-constraint test (it is the reconcile upsert key):

```python
import pytest
from django.db import IntegrityError, transaction

from django_absurd.pg_cron.models import ScheduledTask


def test_scheduledtask_has_explicit_option_columns():
    task = ScheduledTask.objects.create(
        name="nightly", alias="default", task="demo.tasks.ping", cron="0 2 * * *",
        queue="default", args=[1, 2], kwargs={"k": "v"}, max_attempts=3,
        retry_strategy={"kind": "fixed"}, headers={"x": "y"},
        cancellation={"policy": "none"}, idempotency_key="abc",
    )
    task.refresh_from_db()
    assert task.args == [1, 2]
    assert task.kwargs == {"k": "v"}
    assert task.max_attempts == 3
    assert task.retry_strategy == {"kind": "fixed"}
    assert task.headers == {"x": "y"}
    assert task.cancellation == {"policy": "none"}
    assert task.idempotency_key == "abc"
    assert str(task) == "settings:default:nightly"


def test_scheduledtask_option_columns_default_empty():
    task = ScheduledTask.objects.create(
        name="x", alias="default", task="demo.tasks.ping", cron="* * * * *"
    )
    task.refresh_from_db()
    assert task.args == []
    assert task.kwargs == {}
    assert task.max_attempts is None
    assert task.retry_strategy is None
    assert task.headers is None
    assert task.cancellation is None
    assert task.idempotency_key is None


@pytest.mark.django_db(transaction=True)
def test_scheduledtask_unique_per_source_alias_name():
    ScheduledTask.objects.create(
        name="dup", source="settings", alias="default",
        task="demo.tasks.ping", cron="* * * * *",
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        ScheduledTask.objects.create(
            name="dup", source="settings", alias="default",
            task="demo.tasks.ping", cron="* * * * *",
        )
    # cross-source with the same alias/name is allowed
    ScheduledTask.objects.create(
        name="dup", source="admin", alias="default",
        task="demo.tasks.ping", cron="* * * * *",
    )
```

- [ ] **Step 2: RED — sync-rows writes named columns**

In `tests/test_pg_cron_sync_rows.py`, replace `row.params`/`row.options` assertions with
the named columns, and add (reuse the file's existing `tasks()`/schedule helper for the
arrange — spell it out, don't elide):

```python
def test_sync_writes_named_option_columns(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": {"default": {}},
            "OPTIONS": {
                "SCHEDULE": {
                    "nightly": {
                        "task": "tests.tasks.capped",  # decorated max_attempts=3
                        "cron": "0 2 * * *",
                        "args": [1, 2],
                        "kwargs": {"k": "v"},
                    },
                },
            },
        },
    }
    backend = get_absurd_backends()["default"]  # match the import already used in this module
    sync_crons(backend)
    row = ScheduledTask.objects.get(source="settings", alias="default", name="nightly")
    assert row.args == [1, 2]
    assert row.kwargs == {"k": "v"}
    assert row.max_attempts == 3
```

(Confirm `tests.tasks.capped` carries `@absurd_default_params(max_attempts=3)`; if the
fixture task is named differently, use that name and its decorator value.)

- [ ] **Step 3: RED — wrapper reassembles options from columns (e2e)**

`tests/test_run_scheduled_fn.py` has NO `pg_cron` marker (the wrapper fn needs no
extension) — run it plainly. Update it to insert a `ScheduledTask` with named columns
and fire the wrapper, keeping the existing task-side-effect (Payload) assertion for
`args`/`kwargs`, and add a row-level check that the reassembled options reach
`spawn_task` (this one inspects the spawned task row because `max_attempts` isn't
observable via worker side effects — note that in a comment). Rename the file's
leading-underscore `_run` helper to a verb (`fire_wrapper`).

- [ ] **Step 4: Run the three tests — verify RED**

Run: `uv run pytest tests/test_scheduledtask_model.py tests/test_run_scheduled_fn.py -v`
and `uv run pytest tests/test_pg_cron_sync_rows.py -v -m pg_cron`. Expected: model test
`ImportError: cannot import name 'ScheduledTask'`; sync-rows/wrapper tests error at row
construction (`FieldError`/unknown field `args`) because the model still has
`params`/`options`. This whole-file breakage is expected — do not "repair" the old-field
tests; they get rewritten by the implementation phase.

- [ ] **Step 5: Implement the coupled change (prose)**

Do these together (they must land as one working state):

1. **Model** (`models.py`): rename class → `ScheduledTask`; update `__all__`; keep
   `name/source/alias/task/cron/enabled/queue/created_at/updated_at`; remove
   `params`+`options`; add `args=JSONField(default=list)`,
   `kwargs=JSONField(default=dict)`, `max_attempts=IntegerField(null=True, blank=True)`,
   `retry_strategy=JSONField(null=True, blank=True)`,
   `headers=JSONField(null=True, blank=True)`,
   `cancellation=JSONField(null=True, blank=True)`,
   `idempotency_key=TextField(null=True, blank=True)`;
   `Meta.db_table="django_absurd_scheduledtask"`; keep `app_label` + `unique_together`;
   leave the existing accurate `app_label` comment as-is; `__str__` unchanged.
2. **reconcile.py**: rename the `ScheduledJob` import + every usage to `ScheduledTask`;
   in `sync_crons`' `update_or_create`, replace the `params`/`options` defaults with
   `args=schedule.args, kwargs=schedule.kwargs` and split
   `resolve_spawn_options(backend, schedule)` into
   `max_attempts=opts.get("max_attempts"), retry_strategy=opts.get("retry_strategy"), headers=opts.get("headers"), cancellation=opts.get("cancellation"), idempotency_key=opts.get("idempotency_key")`.
   `resolve_spawn_options`/`get_effective_queue` bodies unchanged.
3. **absurd_sync_crons.py**: reword the `--teardown` help text "ScheduledJob
   rows"→"ScheduledTask rows".

- [ ] **Step 6: Regenerate the migration + wrapper (prose)**

Preferred mechanism: temporarily let `makemigrations django_absurd_pg_cron` regenerate
the `CreateModel` for the new fields, then splice the
`RunSQL(sql=CREATE_FN, reverse_sql=DROP_FN)` operation back in and keep `initial=True` +
the `("django_absurd", "0001_initial_0_4_0")` dependency — so field deconstruction
(choices, defaults, `blank`) is exact. Rewrite `CREATE_FN`: `v` is
`public.django_absurd_scheduledtask%ROWTYPE`; **in the body, after `SELECT … INTO v` and
the `IF NOT FOUND OR NOT v.enabled THEN RETURN; END IF;` guard**, assemble:

- `v_params := jsonb_build_object('args', v.args, 'kwargs', v.kwargs);` (args/kwargs are
  NOT NULL — no COALESCE needed)
- `v_options := '{}'::jsonb;` then one guarded append per option:
  `IF v.max_attempts IS NOT NULL THEN v_options := v_options || jsonb_build_object('max_attempts', v.max_attempts); END IF;`
  (repeat for `retry_strategy`, `headers`, `cancellation`, `idempotency_key`).
- `PERFORM absurd.spawn_task(v.queue, v.task, v_params, v_options);` Declare
  `v_params jsonb; v_options jsonb;` in `DECLARE` (declaration only — assignment in the
  body). `DROP_FN` needs no change (signature unchanged). Keep
  `SET search_path = pg_catalog` + full qualification.

- [ ] **Step 7: Run all touched tests — verify GREEN**

Rewrite the remaining `ScheduledJob`/`params`/`options` references in
`tests/test_orm_models.py`, `tests/test_pg_cron_post_migrate.py`,
`tests/test_pg_cron_teardown.py`, `tests/test_absurd_sync_crons_command.py`. Then: Run:
`uv run pytest tests/test_scheduledtask_model.py tests/test_run_scheduled_fn.py -v`
(GREEN); `uv run pytest -m pg_cron -q` (GREEN);
`uv run python -m django makemigrations --check --settings tests.settings` (clean).

- [ ] **Step 8: Sweep + full verify + commit**

`grep -rn "ScheduledJob\|scheduledjob\|\.params\b\|\.options\b" django_absurd/pg_cron tests`
→ only intended hits. Run `uv run pytest -q` (still deselects pg_cron until Task 2 —
pass `-m pg_cron` separately) + `uv run pytest tests/multidb -q`; pre-commit green;
changed lines covered.

```bash
git add -A && git commit -m "feat(pg_cron): ScheduledTask model with explicit option columns

Rename ScheduledJob->ScheduledTask and replace the params/options JSON
blobs with named columns (args, kwargs, max_attempts, retry_strategy,
headers, cancellation, idempotency_key). The wrapper fn reassembles the
params/options jsonb from the columns server-side after reading the row;
injection stays closed."
```

---

### Task 2: Run pg_cron tests by default; fold the pgcron tox env

Annotations #6 + #7. The compose db (and the tox-matrix db) is built from
`Dockerfile.pg_cron`, so the extension is present and the marked tests belong in the
default run.

**Files:**

- Modify: `pyproject.toml` (`[tool.pytest.ini_options].addopts` — remove the
  `"-m"`/`"not pg_cron"` pair; keep the `pg_cron` marker registration + reword its help)
- Modify: `tox.ini` (`[testenv].commands` — merge the two matrix pytest passes into one)
- Modify: `tests/conftest.py` (fix the `ensure_pg_cron` docstring that says the default
  run "deselects pg_cron")

- [ ] **Step 1: Drop the default deselect + fix docstring (prose)**

`pyproject.toml`: remove the `"-m"` and `"not pg_cron"` entries from `addopts`. Keep the
`pg_cron` marker under `markers`; reword its help from "deselected by default (run with
-m pg_cron …)" to note it now runs by default and can be excluded with
`-m "not pg_cron"`. `tests/conftest.py`: update `ensure_pg_cron`'s docstring — the
default `uv run pytest` now DOES need the extension; `ensure_pg_cron` still
`CREATE EXTENSION`s and hard-errors on a non-pg_cron Postgres. This is the intended
trade-off: the compose db (or a pg_cron Postgres) is now required for the default suite
(CI builds it; CLAUDE.md updated in Task 5), not a graceful skip.

- [ ] **Step 2: Verify default suite now runs pg_cron**

Run: `uv run pytest -q` (compose db up). Expected: PASS; the pg_cron count is folded
into the default run (≈ prior default + prior pg_cron); nothing deselected for the
`pg_cron` marker. Don't chase an exact number.

- [ ] **Step 3: Fold the tox matrix pytest passes (prose)**

`tox.ini` `[testenv].commands`: replace
`!mypy: pytest -m "not packaging and not pg_cron" {posargs}` **and**
`!mypy: pytest --cov-append -m pg_cron {posargs}` with a single
`!mypy: pytest -m "not packaging" {posargs}` (pg_cron now included; the `--cov-append`
was only needed to stitch the split). Keep the `pytest tests/multidb {posargs}` line and
the `mypy` line. Update the inline comment.

- [ ] **Step 4: Verify + commit**

Run: `uvx --with tox-uv tox -e py312-django60` — confirm the folded command runs pg_cron
tests once with coverage; `tests/multidb` still runs separately.

```bash
git add pyproject.toml tox.ini tests/conftest.py && git commit -m "test: run pg_cron suite by default; fold pgcron tox pass

The compose/tox db is pg_cron-enabled, so the marked tests belong in the
default run. Drop the addopts deselect, merge the separate tox pass, and
note that a pg_cron Postgres is now required for the default suite."
```

---

### Task 3: Example Dockerfile installs from `examples/pyproject.toml`

Annotation #4. The `RUN` line hardcodes `django==6.0.6`/`psycopg[binary]==3.3.4` while
the file's own comment says deps come from `examples/pyproject.toml` (which declares
them + django-absurd as an editable path source `{path = "..", editable = true}`).

**Files:**

- Modify: `examples/Dockerfile` (install from the example pyproject; drop inline pins)

- [ ] **Step 1: Switch to pyproject-driven install (prose)**

Two naive approaches fail and must be avoided: (a) `uv pip install .` builds the
flat-layout example project itself (top-level `config/`+`demo/` → setuptools
auto-discovery error, no build backend); (b) copying `examples/pyproject.toml` to `/app`
breaks its `{path=".."}` source (`..` from `/app` is `/`, not the package root). Working
mechanism: the image already copies the django-absurd package (`pyproject.toml`,
`README.md`, `django_absurd/`) to `/src`. Also copy `examples/pyproject.toml` to
`/src/examples/pyproject.toml` so its `{path=".."}` resolves to `/src` (the package
root). Then install the example's dependencies (not the example project itself) into
`/opt/venv`, e.g.
`UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --no-install-project --project /src/examples`.
Django/psycopg versions + the editable django-absurd path source now come from
`examples/pyproject.toml` only; drop the inline `"django==…" "psycopg…"` pins. The
run-time bind mount (compose) still overlays the live app source. Keep
`SETUPTOOLS_SCM_PRETEND_VERSION` (hatch-vcs needs it; no `.git` in context).

- [ ] **Step 2: Live-verify the build + boot**

Run (from `examples/`, after `docker compose down -v`): `docker compose up --build`;
confirm migrate applies and the worker logs `pong 🏓`. If Docker is unavailable, say so
and static-check the Dockerfile ↔ example pyproject resolution instead — do not claim a
run you didn't do.

- [ ] **Step 3: Commit**

```bash
git add examples/Dockerfile && git commit -m "examples: install deps from examples/pyproject.toml, not inline pins"
```

---

### Task 4: Demo beat + pg_cron in one project (one DB, two backends)

Annotation #5, reshaped to fit the single-absurd-DB limit. One database; two
`AbsurdBackend`s with **distinct queues** — the existing `"default"` backend stays
pg_cron (queue `default`), a new backend runs beat (queue `beat`). `resolve_backend`
requires `--alias` once multiple backends exist, so the compose worker commands MUST
pass `--alias`/`--queue`.

**Files:**

- Modify: `examples/config/settings.py` (second `TASKS` backend, same `DATABASE`/db,
  `SCHEDULER="beat"`, own `QUEUES`+`SCHEDULE`)
- Modify: `examples/demo/tasks.py` (add a `tick` task logging a distinct line, e.g.
  `tock ⏰`)
- Modify: `examples/compose.yaml` (existing `worker` gains
  `--alias default --queue default`; add a `beatworker` service running
  `--alias beat --queue beat --beat`)
- Modify: `examples/README.md` (Task 5 covers prose; keep run steps accurate here)

- [ ] **Step 1: Add the beat backend + tick task (prose)**

`examples/config/settings.py`: add a second `TASKS` entry (e.g. `"beat"`) →
`BACKEND=AbsurdBackend`,
`OPTIONS={"QUEUES": {"beat": {}}, "SCHEDULER": "beat", "SCHEDULE": {"tick": {"task": "demo.tasks.tick", "cron": "* * * * *", "queue": "beat"}}}`.
Both backends use the default database (no `DATABASE` override) → E004 does NOT fire
(single database). The `"default"` backend stays `SCHEDULER="pg_cron"` (queue
`default`). Add `demo.tasks.tick` logging a distinct line (`tock ⏰`). Confirm
`manage.py check` is clean (two backends, one DB).

- [ ] **Step 2: Wire compose services (prose)**

`examples/compose.yaml`: the existing pg_cron `worker` command becomes
`python manage.py absurd_worker --alias default --queue default` (required now that two
backends exist). Add a `beatworker` service:
`python manage.py absurd_worker --alias beat --queue beat --beat` (co-located
worker+beat for the beat backend), same env/mounts, depending on `migrate` completion.
Keep the single `db` service (pg_cron image; `cron.database_name=demo`). `migrate` runs
once on the default database (both backends live there — no second migrate needed).

- [ ] **Step 3: Live-verify both schedulers**

Run (from `examples/`, after `docker compose down -v`): `docker compose up --build`.
Within ~2 minutes confirm BOTH `pong 🏓` (pg_cron, `default` queue) and `tock ⏰` (beat,
`beat` queue) appear. If Docker unavailable, say so and static-check settings/compose
consistency.

- [ ] **Step 4: Commit**

```bash
git add examples && git commit -m "examples: demo beat + pg_cron in one project (two backends, one db)"
```

---

### Task 5: Docs sweep (sync-docs)

Reflect the rename + named columns + dual-scheduler example + default-test behavior.
Invoke the project `sync-docs` skill's audience map.

**Files:**

- Modify: `django_absurd/AGENTS.md` (ScheduledTask; wrapper reads named columns;
  projection table's explicit option columns; the `_normalize_spawn_options`
  falsy-vs-NULL edge note; both-scheduler mention)
- Modify: `docs/web/cron-jobs.md` (projection-table description → named columns; example
  demos both schedulers)
- Modify: `docs/web/configuration.md` (only if it names the model/table)
- Modify: `docs/WHY.md` (line ~86 references the `ScheduledJob` projection table →
  `ScheduledTask`)
- Modify: `examples/README.md` (two backends, `docker compose down -v` on first
  run/upgrade, `--alias` worker commands, both expected log lines)
- Modify: `CLAUDE.md` (testing section: pg_cron tests now run in the default
  `uv run pytest`; a pg_cron Postgres is required; opt out with `-m "not pg_cron"`)
- Modify: `README.md` (keep trim; fix links/blurb only if stale)

- [ ] **Step 1: Update AGENTS + cron-jobs + configuration + WHY (prose)**

Replace `ScheduledJob`→`ScheduledTask`; describe the projection table's explicit option
columns
(`args, kwargs, max_attempts, retry_strategy, headers, cancellation, idempotency_key`)
and that the wrapper reassembles `params`/`options` from them; keep the `public`-schema
rationale; state admin stays read-only (settings is source of truth). Add one line in
AGENTS.md noting a directly-inserted (non-settings) row storing an empty `{}` in
`retry_strategy`/`cancellation` would pass through the wrapper's `IS NOT NULL`, whereas
the reconcile path never stores `{}` — so settings rows are unaffected.

- [ ] **Step 2: Update example + CLAUDE.md + README (prose)**

`examples/README.md`: two backends/two queues, `docker compose down -v` on first
run/upgrade, the `--alias` worker + `beatworker` services, both expected log lines.
`CLAUDE.md` testing section: drop "single-DB suite / `-m pgcron`" deselection wording;
pg_cron tests run in the default `uv run pytest` and need a pg_cron Postgres; opt out
with `-m "not pg_cron"`. `README.md`: verify links resolve; no prose bloat.

- [ ] **Step 3: Cross-check + commit**

Grep the docs for stray `ScheduledJob`, `params`/`options` blob mentions, "deselected by
default", and single-DB scheduler wording; confirm command/flag/id/table copy matches
code. Run `uv run pytest -q` (docs shouldn't change it) + pre-commit.

```bash
git add -A && git commit -m "docs: ScheduledTask + explicit option columns; dual-scheduler example; pg_cron tests default"
```

---

## Self-Review

**Spec coverage:** #1 (Global Constraints — keep `absurd.*`) ✓; #2 (Task 1 rename) ✓; #3
(Task 1 columns) ✓; #4 (Task 3) ✓; #5 (Task 4, one-DB/two-backend per the resolved core
limit) ✓; #6 (Task 2 addopts) ✓; #7 (Task 2 tox) ✓; docs incl. WHY.md (Task 5) ✓.

**Type consistency:** `ScheduledTask` + column names identical across model (Step 5.1),
migration (Step 6), reconcile (Step 5.2), wrapper (Step 6), tests. Option column set ==
`_normalize_spawn_options` keys + `args`/`kwargs`.
`resolve_spawn_options`/`get_effective_queue` signatures unchanged.
`unique_together = (("source","alias","name"),)` matches the live nested-tuple form.

**Ordering:** Task 1 lands the rename+reshape as one coupled phase (no broken
`django.setup()` checkpoint); RED tests precede it and the real intermediate breakage
(`FieldError`, whole-file RED) is stated. Task 1 uses `-m pg_cron` explicitly (addopts
still deselects until Task 2). Task 4 requires `--alias` on worker commands (two
backends). Task 5 runs last.

**Placeholder scan:** implementation steps are prose-with-exact-names (project rule: no
pre-written production code in plans); test steps carry full RED code with spelled-out
arranges. No TBD/TODO.
