# Admin-writable pg_cron schedules (Phase B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** author/edit/delete recurring pg_cron schedules in Django admin as
`source="admin"` `ScheduledTask` rows, with the pg_cron job (un)scheduled automatically
whenever such a row is saved or deleted.

**Architecture:** Phase A extracted the validators and enforces them model-first
(`ScheduledTask.clean()` + field `validators`). Phase B adds: DB-authoritative cron
validation, self-exclusion on the cross-source clash validator, **automatic pg_cron job
emission on every `ScheduledTask` save/delete** (a `post_save`/`post_delete` signal —
one central path, so admin HTTP writes, direct ORM writes, and bulk writes are all
covered), and a writable `ScheduledTaskAdmin` (its own `ModelAdmin` + `ModelForm`,
per-object gating: `settings` rows read-only, `admin` rows editable). Tearing down admin
jobs is a **guarded management-command action** (never automatic).

**Tech Stack:** Django 6.0 admin (ModelAdmin/ModelForm, per-object permissions,
`get_readonly_fields`), psycopg3, pg_cron 1.6
(`cron.schedule`/`cron.unschedule`/`cron.alter_job`), pytest (function-based), Django
test `client` for admin HTTP, BeautifulSoup for HTML assertions.

## Global Constraints

- Runtime floor: **Django 6.0 / Python 3.12**; psycopg (v3) backend only.
- **`import typing as t`** — never `from typing import X`. **Absolute imports only** (no
  relative).
- Functions must contain a verb; no leading-underscore module constants/helpers; helpers
  live BELOW the public function that uses them. **A `ScheduledTask` instance is
  `scheduled_task`, never `row`.**
- **Never pre-write production code** in this plan — steps show failing tests (RED) then
  describe minimal implementation in prose.

### Testing rules (this feature kept breaking these — hold them hard)

- **Test high-level / behavioral, through REAL entrypoints — never unit-call our own
  functions.** A validation rule is proven through the entrypoint that runs it — the
  **parametrized subject harness** in `tests/pg_cron/validators/` — not a bespoke
  standalone test. Admin behavior is driven through the **HTTP request cycle**. A
  management-command behavior is driven through `call_command(...)`. Never test by
  calling `teardown_crons()` / a validator / an emitter directly.
- **Inventory and REUSE existing test infrastructure before writing anything.** For
  `tests/pg_cron/`:
  - `get_managed_cron_jobs` fixture (`tests/pg_cron/conftest.py`) — returns
    `(jobname, schedule, command, active)` tuples for all managed jobs.
  - **`get_scheduled_cron_job` fixture (added in Task 3)** —
    `get_scheduled_cron_job(alias, name, source="admin")` returns the single matching
    job tuple (or `None`); builds the jobname itself. Use this whenever a test wants one
    specific job — never repeat `build_jobname(...)` + a comprehension over
    `get_managed_cron_jobs()`.
  - autouse `_clear_owned_pg_cron_jobs` cleanup — no manual job teardown in tests.
  - **`build_pg_cron_tasks(schedule)` / `build_beat_tasks(schedule)`** — promoted into
    `tests/pg_cron/conftest.py` in Task 3; the whole suite shares one builder.
  - `configure_pg_cron_backend` + the `validate_from_model` /
    `validate_from_system_check` adapters in `tests/pg_cron/validators/`.
- **No invented abstractions.** No local `make()`/`job()`/`configure()` factories; no
  wrapper error messages nobody asked for.
- **Assert the COMPLETE literal message**, never a fragment (`"cron" in message_dict` is
  banned). Our validator messages: assert the exact known string. The cron error is
  pg_cron's own text surfaced verbatim; pin the complete string captured on the RED run.
- pytest **function-based only**; **no monkeypatch / `unittest.mock`** (to reach a
  branch, drive it with a real input — e.g. a `StringIO` on `sys.stdin` for a
  confirmation prompt); **alphabetize** `@pytest.mark.parametrize` values and fixture
  `params`.
- `@pytest.mark.django_db(transaction=True)` for anything that commits/DDLs (all
  `cron.*`, admin POST that emits, the rolled-back cron probe) — the pg_cron suite
  already uses it.
- **Full patch coverage**: 100% statement+branch on added/changed lines.
- pg_cron suite: `docker compose up -d db db_pg_cron`; `uv run pytest tests/pg_cron`;
  `--reuse-db` (never `--create-db`).
- Validator single-source-of-truth: a rule's message must not diverge across the
  entrypoints that enforce it.
- New feature on a GitHub repo → stay LOCAL, commit incrementally; do not push/PR until
  asked.

## File Structure

- `django_absurd/pg_cron/validators.py` (modify) — add
  `validate_pg_cron_cron(cron, database)`; add `pk`/self-exclusion to
  `validate_no_cross_source_clash`.
- `django_absurd/pg_cron/models.py` (modify) — `clean()` calls the cron validator
  (backend-resolved else-branch) and passes `self.pk` to the clash validator.
- `django_absurd/pg_cron/reconcile.py` (modify) — add
  `schedule_pg_cron_job(scheduled_task)` / `unschedule_pg_cron_job(scheduled_task)`
  (unschedule reuses `prune_pg_cron_jobs`); add an `include_admin=False` param to
  `teardown_crons` so ONLY the guarded command clears admin jobs.
- `django_absurd/pg_cron/signals.py` (create) — `post_save`/`post_delete` receivers
  scoped to `source="admin"` calling the emitters; connected in `apps.py`.
- `django_absurd/pg_cron/apps.py` (modify) — connect the two signals in `ready()`.
- `django_absurd/pg_cron/management/commands/absurd_sync_crons.py` (modify) —
  `--teardown` gains a `--no-input` guard + confirmation prompt and clears admin jobs
  (`include_admin=True`).
- `django_absurd/pg_cron/admin.py` (modify) — replace read-only `ScheduledTaskAdmin`
  with a writable one (own base, per-object gating) + `ScheduledTaskForm`.
- `tests/pg_cron/conftest.py` (modify) — add `get_scheduled_cron_job`; host
  `build_pg_cron_tasks` / `build_beat_tasks`.
- `tests/pg_cron/validators/` (modify) — `test_cron.py` (create, harness rule);
  `utils.py` + `conftest.py` add the admin-POST subject + a model+form fixture.
- `tests/pg_cron/` (create/modify) — `test_schedule_emission.py`;
  `test_absurd_sync_crons_command.py` (add guarded-teardown cases);
  `test_admin/test_scheduledtask.py` (read-only → writable).
- `django_absurd/AGENTS.md` + `docs/web/scheduling.md` + `zensical.toml` (modify) —
  document admin-authored schedules.

**Cross-task interfaces (defined once):**

- `validate_pg_cron_cron(cron: str, database: str) -> None` — asks pg_cron to schedule a
  throwaway job inside an always-rolled-back transaction; on rejection raises
  `ValidationError({"cron": [<pg_cron's own message, verbatim>]})`.
- `validate_no_cross_source_clash(source, alias, name, pk=None)` — `pk` excludes the row
  under edit.
- `schedule_pg_cron_job(scheduled_task)` / `unschedule_pg_cron_job(scheduled_task)` —
  the central emitters, called ONLY by the signal receivers. Job name
  `absurd:admin:<alias>:<name>`; `alter_job(active := scheduled_task.enabled)`; row's
  backend DB; under `SYNC_CRONS_ADVISORY_LOCK`.
- `teardown_crons(backend, include_admin=False)` — `include_admin=True` (guarded command
  only) also clears `absurd:admin:<alias>:*` jobs.
- `get_scheduled_cron_job(alias, name, source="admin")` — test fixture returning one job
  tuple or `None`.
- `build_pg_cron_tasks(schedule)` — shared TASKS builder (conftest, Task 3).
- `build_jobname` / `build_jobname_prefix` / `SYNC_CRONS_ADVISORY_LOCK` /
  `SCHEDULE_JOB_SQL` / `prune_pg_cron_jobs` — already in place.

---

### Task 1: Self-exclusion on the cross-source clash validator

**Files:** Modify `validators.py` (`validate_no_cross_source_clash`), `models.py`
(`clean()` passes `self.pk`). Test:
`tests/pg_cron/validators/test_cross_source_clash.py`.

**Why:** Phase A's validator has a documented gap: no pk exclusion. Editing a persisted
admin `scheduled_task` re-runs `clean()`; the clash query must exclude the row under
edit. Tested behaviorally via `full_clean()` on a persisted instance — not a direct
validator call.

- [ ] **Step 1: Write the failing test** — add to
      `tests/pg_cron/validators/test_cross_source_clash.py`:

```python
def test_revalidating_a_saved_admin_schedule_does_not_self_clash(settings):
    from tests.pg_cron.validators.utils import configure_pg_cron_backend
    from django_absurd.pg_cron.models import ScheduledTask

    configure_pg_cron_backend(settings)
    scheduled_task = ScheduledTask.objects.create(
        source="admin", alias="default", name="nightly",
        task="tests.tasks.add", cron="0 2 * * *",
    )
    scheduled_task.enabled = False
    scheduled_task.full_clean()  # raises ValidationError if it clashes with itself
```

- [ ] **Step 2: RED** —
      `uv run pytest tests/pg_cron/validators/test_cross_source_clash.py::test_revalidating_a_saved_admin_schedule_does_not_self_clash -q`
      → FAIL (saved row self-clashes).
- [ ] **Step 3: Implement (prose)** — add `pk: t.Any = None` to
      `validate_no_cross_source_clash`; when set, `.exclude(pk=pk)`. Drop the "no pk
      exclusion" docstring caveat. `ScheduledTask.clean()` passes `self.pk`.
- [ ] **Step 4: GREEN** —
      `uv run pytest tests/pg_cron/validators/test_cross_source_clash.py -q` → PASS
      (existing rejection test still asserts the complete
      `"a settings schedule 'nightly' already exists on backend 'default'."`).
- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/validators.py django_absurd/pg_cron/models.py tests/pg_cron/validators/test_cross_source_clash.py
git commit -m "feat(pg_cron): self-exclusion on cross-source schedule clash validator"
```

---

### Task 2: DB-authoritative cron validation (folded into the validator harness)

**Files:** Modify `validators.py` (add `validate_pg_cron_cron`), `models.py` (`clean()`
calls it). Test: `tests/pg_cron/validators/test_cron.py` (create — a harness rule file).

**Why:** pg_cron owns its grammar (5-field cron AND `[1-59] seconds`; rejects `1 hour`).
Ask pg_cron directly: schedule a throwaway job inside an always-rolled-back transaction;
on rejection surface pg_cron's own message verbatim on the `cron` field (no invented
wrapper). **This is a validation rule, so it lives in the parametrized validator
harness** — enforced by the `model` subject now, and broadened to `form` in Task 6. It
is NOT a `check`-subject rule: the system check deliberately does not validate pg_cron
grammar (DB-authoritative, Phase A).

**How it works:** open a transaction on the backend DB,
`cron.schedule('<throwaway name with "probe">', <cron>, 'select 1')`, force rollback so
nothing persists. A `DatabaseError`/`ProgrammingError`/`InternalError` ⇒ raise
`ValidationError({"cron": [<pg_cron's message>]})`. Runs during admin `form.is_valid()`,
OUTSIDE the admin save-time transaction, so it opens its OWN transaction. Match on
exception type, not text.

- [ ] **Step 1: Write the failing rule test** — create
      `tests/pg_cron/validators/test_cron.py`, using the Phase A `validate_from_model`
      adapter (the `model` subject); pin the exact pg_cron message on the RED run:

```python
import pytest

from tests.pg_cron.validators.utils import validate_from_model

pytestmark = pytest.mark.django_db(transaction=True)

# exact pg_cron 1.6 message for an invalid expression — pin from the RED run
PG_CRON_BAD_CRON_MESSAGE = "<complete pg_cron error text, captured at RED>"

BAD = ["* * *", "1 hour", "not a cron"]
GOOD = ["*/5 * * * *", "0 2 * * *", "30 seconds"]


@pytest.mark.parametrize("cron", GOOD)
def test_valid_pg_cron_expression_accepted(settings, cron):
    result = validate_from_model(settings, cron=cron)
    assert not result or PG_CRON_BAD_CRON_MESSAGE not in result


@pytest.mark.parametrize("cron", BAD)
def test_invalid_pg_cron_expression_rejected(settings, cron):
    result = validate_from_model(settings, cron=cron)
    assert result
    assert PG_CRON_BAD_CRON_MESSAGE in result
```

- [ ] **Step 2: RED** — `uv run pytest tests/pg_cron/validators/test_cron.py -q` → FAIL
      (`clean()` does not reject bad pg_cron crons yet). Capture pg_cron's real message;
      pin `PG_CRON_BAD_CRON_MESSAGE`.
- [ ] **Step 3: Implement (prose)** — add `validate_pg_cron_cron(cron, database)` per
      "How it works" (surface pg_cron's message verbatim). In `ScheduledTask.clean()`,
      inside the backend-resolved `else` branch (after
      `validate_alias_is_pg_cron_backend`), call it with `backend.database` and fold a
      raised error into `errors["cron"]`.
- [ ] **Step 4: GREEN** — `uv run pytest tests/pg_cron/validators/test_cron.py -q` →
      PASS. (Task 6 adds the `form` subject to this same rule file.)
- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/validators.py django_absurd/pg_cron/models.py tests/pg_cron/validators/test_cron.py
git commit -m "feat(pg_cron): DB-authoritative cron validation via pg_cron"
```

---

### Task 3: Automatic pg_cron job emission on save/delete

**Files:** Create `signals.py`. Modify `reconcile.py` (`schedule_pg_cron_job` /
`unschedule_pg_cron_job`), `apps.py` (connect signals), `tests/pg_cron/conftest.py` (add
`get_scheduled_cron_job`; promote `build_pg_cron_tasks` / `build_beat_tasks` here).
Test: `tests/pg_cron/test_schedule_emission.py`.

**Why:** Saving a `source="admin"` `scheduled_task` schedules its pg_cron job; deleting
unschedules. `source="settings"` writes skip (reconcile owns them). Receivers fire
inside the caller's transaction, so a failing pg_cron op rolls the write back. Tested
through `.save()`/`.delete()` + `get_scheduled_cron_job` — never by calling the
emitters.

- [ ] **Step 1: Add shared test infra** — in `tests/pg_cron/conftest.py`: (a) move
      `build_pg_cron_tasks`/`build_beat_tasks` here from
      `test_absurd_sync_crons_command.py` (which now imports them); (b) add the
      `get_scheduled_cron_job` fixture:

```python
@pytest.fixture
def get_scheduled_cron_job():
    """Return get(alias, name, source="admin") -> the (jobname, schedule, command,
    active) tuple for that pg_cron job, or None."""
    from django.db import connections
    from django_absurd.pg_cron.validators import build_jobname

    def _get(alias, name, source="admin"):
        with connections["default"].cursor() as cur:
            cur.execute(
                "select jobname, schedule, command, active from cron.job "
                "where jobname = %s",
                [build_jobname(alias, name, source)],
            )
            return cur.fetchone()

    return _get
```

Run `uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py -q` → still PASS
(pure move).

- [ ] **Step 2: Write the failing tests** — create
      `tests/pg_cron/test_schedule_emission.py`:

```python
import pytest

from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.conftest import build_pg_cron_tasks

pytestmark = pytest.mark.django_db(transaction=True)


def test_saving_admin_schedule_schedules_the_job(settings, get_scheduled_cron_job):
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="admin", alias="default", name="nightly",
        task="tests.tasks.add", cron="0 2 * * *", enabled=True,
    )
    _, schedule, _, active = get_scheduled_cron_job("default", "nightly")
    assert schedule == "0 2 * * *"
    assert active is True


def test_saving_disabled_admin_schedule_is_inactive(settings, get_scheduled_cron_job):
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="admin", alias="default", name="paused",
        task="tests.tasks.add", cron="0 2 * * *", enabled=False,
    )
    assert get_scheduled_cron_job("default", "paused")[3] is False


def test_saving_settings_schedule_does_not_schedule_a_job(settings, get_scheduled_cron_job):
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="settings", alias="default", name="owned_by_reconcile",
        task="tests.tasks.add", cron="0 2 * * *",
    )
    assert get_scheduled_cron_job("default", "owned_by_reconcile", source="settings") is None


def test_deleting_admin_schedule_unschedules_the_job(settings, get_scheduled_cron_job):
    settings.TASKS = build_pg_cron_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="admin", alias="default", name="gone",
        task="tests.tasks.add", cron="0 2 * * *",
    )
    assert get_scheduled_cron_job("default", "gone") is not None
    scheduled_task.delete()
    assert get_scheduled_cron_job("default", "gone") is None
```

- [ ] **Step 3: RED** — `uv run pytest tests/pg_cron/test_schedule_emission.py -q` →
      FAIL (saving schedules nothing).
- [ ] **Step 4: Implement (prose)** — in `reconcile.py`:
      `schedule_pg_cron_job(scheduled_task)` resolves the backend via
      `get_absurd_backends()[scheduled_task.alias]`, opens
      `transaction.atomic(using=backend.database)`, acquires `SYNC_CRONS_ADVISORY_LOCK`,
      runs `SCHEDULE_JOB_SQL` with
      `build_jobname(scheduled_task.alias, scheduled_task.name, source="admin")` +
      `scheduled_task.cron` + wrapper args
      `("admin", scheduled_task.alias, scheduled_task.name)`, then
      `cron.alter_job(jobid, active := scheduled_task.enabled)`.
      `unschedule_pg_cron_job(scheduled_task)` finds the jobid by that jobname and
      **reuses `prune_pg_cron_jobs`**. Create `signals.py` with two receivers returning
      early unless `instance.source == "admin"`; connect `post_save`/`post_delete`
      (`sender=ScheduledTask`) in `apps.py` `ready()` (lazy import). Do not swallow
      emitter exceptions.
- [ ] **Step 5: GREEN** — `uv run pytest tests/pg_cron/test_schedule_emission.py -q` →
      PASS.
- [ ] **Step 6: Commit**

```bash
git add django_absurd/pg_cron/reconcile.py django_absurd/pg_cron/signals.py django_absurd/pg_cron/apps.py tests/pg_cron/conftest.py tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_schedule_emission.py
git commit -m "feat(pg_cron): auto-schedule pg_cron job on admin ScheduledTask save/delete"
```

---

### Task 4: Guarded teardown of admin jobs (management command only)

**Files:** Modify `reconcile.py` (`teardown_crons(backend, include_admin=False)`),
`django_absurd/pg_cron/management/commands/absurd_sync_crons.py` (`--no-input` +
confirmation, pass `include_admin=True`). Test:
`tests/pg_cron/test_absurd_sync_crons_command.py` (add cases).

**Why (decision):** Tearing down admin jobs is destructive to user-authored schedules,
so it is NOT automatic. Migrate-time teardown
(`reconcile_crons_after_migrate → teardown_crons(backend)`) stays settings-only. Only
`absurd_sync_crons --teardown` clears admin jobs, and only after an interactive
confirmation (bypass `--no-input`). Admin ROWS are kept; settings rows are deleted as
today. Driven through `call_command` — the real entrypoint — never `teardown_crons()`
directly.

- [ ] **Step 1: Write the failing tests** — add to
      `tests/pg_cron/test_absurd_sync_crons_command.py` (reuse `build_pg_cron_tasks`,
      `get_scheduled_cron_job`; feed the prompt via a real `StringIO` stdin — not a mock
      — for the abort path):

```python
import io
import sys


def test_teardown_command_clears_admin_job_after_confirmation(
    settings, get_scheduled_cron_job
):
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="admin", alias="default", name="keepme",
        task="tests.tasks.add", cron="0 2 * * *",
    )
    assert get_scheduled_cron_job("default", "keepme") is not None

    call_command("absurd_sync_crons", teardown=True, no_input=True)

    assert get_scheduled_cron_job("default", "keepme") is None
    assert ScheduledTask.objects.filter(source="admin", name="keepme").exists()


def test_teardown_command_aborts_when_confirmation_declined(
    settings, get_scheduled_cron_job
):
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="admin", alias="default", name="keepme",
        task="tests.tasks.add", cron="0 2 * * *",
    )
    original_stdin = sys.stdin
    sys.stdin = io.StringIO("no\n")  # real input drives the abort branch
    try:
        call_command("absurd_sync_crons", teardown=True)
    finally:
        sys.stdin = original_stdin

    assert get_scheduled_cron_job("default", "keepme") is not None  # not torn down
```

- [ ] **Step 2: RED** —
      `uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py -q` → FAIL (no
      `--no-input`/confirmation; admin jobs not cleared by the command).
- [ ] **Step 3: Implement (prose)** — add `include_admin: bool = False` to
      `teardown_crons`; when True, also gather
      `build_jobname_prefix(backend.alias, source="admin")` jobids and prune them under
      the existing advisory lock (settings-row deletion unchanged; admin rows kept). In
      the command: add `--no-input` (`dest="no_input"`); in the `--teardown` branch,
      unless `no_input`, print what will be unscheduled and read a confirmation from
      stdin (`input()`), aborting (write a message, return) on anything but yes; on
      confirm/`--no-input`, call `teardown_crons(backend, include_admin=True)`. Migrate
      path keeps calling `teardown_crons(backend)` (settings-only).
- [ ] **Step 4: GREEN (+ no regression)** —
      `uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_pg_cron_teardown.py -q`
      → PASS.
- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/reconcile.py django_absurd/pg_cron/management/commands/absurd_sync_crons.py tests/pg_cron/test_absurd_sync_crons_command.py
git commit -m "feat(pg_cron): guarded teardown of admin jobs via absurd_sync_crons --teardown"
```

---

### Task 5: Writable ScheduledTaskAdmin + ModelForm (HTTP-tested)

**Files:** Modify `admin.py`; `tests/pg_cron/test_admin/test_scheduledtask.py`
(read-only → writable, all HTTP).

**Why:** One registered `ScheduledTaskAdmin` serves both lanes — `settings` read-only,
`admin` creatable/editable/deletable — via per-object permissions + conditional readonly
fields (NOT by flipping shared `ReadOnlyAdminBase`). Saving emits the job (Task 3), so
the admin has no job code; the POST test asserts the job side effect via
`get_scheduled_cron_job`.

**Design notes (prose):**

- Base `admin.ModelAdmin`. Keep existing
  `list_display`/`list_filter`/`search_fields`/`fieldsets`/`ordering`.
- `has_add_permission → True`. `has_change_permission(request, obj=None)`: True when
  `obj is None` or `obj.source == "admin"`. `has_delete_permission(request, obj=None)`:
  same. `has_view_permission → True`.
- `get_readonly_fields(request, obj)`: settings row → all model fields; existing admin
  row → `("alias", "created_at", "name", "source", "updated_at")` (identity immutable);
  add → `("created_at", "source", "updated_at")`.
- `ScheduledTaskForm(forms.ModelForm)`: `alias` a `ChoiceField` over configured pg_cron
  backends
  (`[(a, a) for a, b in get_absurd_backends().items() if b.scheduler == "pg_cron"]`),
  label `"Backend"`, help text `"Which Absurd pg_cron backend runs this schedule."`,
  initial = sole backend when one exists; `cron` help text carries the high-frequency
  `<n> seconds` caveat.
- `save_model` forces `obj.source = "admin"` on add.

- [ ] **Step 1: Write the failing tests** — in
      `tests/pg_cron/test_admin/test_scheduledtask.py` remove
      `test_no_add_link_and_add_forbidden` and the old
      `test_detail_is_readonly_and_shows_option_columns`; keep
      changelist/columns/filter/search; add HTTP tests. (Do NOT add a pure
      help-text-string assertion — assert the _behavior_ that the Backend field offers
      exactly the configured pg_cron backends.)

```python
def test_add_link_present_and_add_view_renders(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    changelist = client.get(CHANGELIST)
    assert changelist.status_code == 200
    soup = BeautifulSoup(changelist.content, "html.parser")
    assert soup.select_one(".object-tools a.addlink") is not None
    add = client.get(ADD)
    assert add.status_code == 200


def test_add_view_backend_field_offers_only_pg_cron_backends(settings, client, admin_user):
    seed(settings)  # a single pg_cron backend "default"
    client.force_login(admin_user)
    response = client.get(ADD)
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    options = [o.get("value") for o in soup.select('select[name="alias"] option') if o.get("value")]
    assert options == ["default"]


def test_posting_add_creates_admin_row_and_schedules_job(
    settings, client, admin_user, get_scheduled_cron_job
):
    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {
        "alias": "default", "name": "fromadmin", "task": "tests.tasks.add",
        "queue": "", "cron": "0 3 * * *", "enabled": "on",
        "args": "[]", "kwargs": "{}", "max_attempts": "", "retry_strategy": "",
        "headers": "", "cancellation": "", "idempotency_key": "",
    })
    assert response.status_code == 302
    assert ScheduledTask.objects.get(name="fromadmin").source == "admin"
    assert get_scheduled_cron_job("default", "fromadmin") is not None


def test_posting_add_with_invalid_cron_shows_pg_crons_complete_message(
    settings, client, admin_user
):
    from tests.pg_cron.validators.test_cron import PG_CRON_BAD_CRON_MESSAGE

    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {
        "alias": "default", "name": "badcron", "task": "tests.tasks.add",
        "queue": "", "cron": "1 hour", "enabled": "on",
        "args": "[]", "kwargs": "{}", "max_attempts": "", "retry_strategy": "",
        "headers": "", "cancellation": "", "idempotency_key": "",
    })
    assert response.status_code == 200  # re-rendered with errors, not saved
    assert not ScheduledTask.objects.filter(name="badcron").exists()
    assert PG_CRON_BAD_CRON_MESSAGE in response.content.decode()


def test_settings_schedule_detail_is_readonly(settings, client, admin_user):
    seed(settings)
    pk = ScheduledTask.objects.get(name="hourly").pk  # a settings row
    client.force_login(admin_user)
    response = client.get(change_url(pk))
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    assert soup.select_one('input[name="cron"]') is None


def test_admin_schedule_edit_form_cron_editable_name_immutable(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {
        "alias": "default", "name": "editable", "task": "tests.tasks.add",
        "queue": "", "cron": "0 3 * * *", "enabled": "on",
        "args": "[]", "kwargs": "{}", "max_attempts": "", "retry_strategy": "",
        "headers": "", "cancellation": "", "idempotency_key": "",
    })
    pk = ScheduledTask.objects.get(name="editable").pk
    response = client.get(change_url(pk))
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    assert soup.select_one('input[name="cron"]') is not None   # editable
    assert soup.select_one('input[name="name"]') is None       # immutable on edit
```

- [ ] **Step 2: RED** —
      `uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py -q` → FAIL (add
      currently 403).
- [ ] **Step 3: Implement (prose)** — rewrite `ScheduledTaskAdmin` per the design notes.
      Registration functions unchanged.
- [ ] **Step 4: GREEN** — `uv run pytest tests/pg_cron/test_admin/ -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/admin.py tests/pg_cron/test_admin/test_scheduledtask.py
git commit -m "feat(pg_cron): writable ScheduledTask admin for source=admin schedules"
```

---

### Task 6: Admin-POST subject in the validator harness (+ cron gains it)

**Files:** Modify `tests/pg_cron/validators/utils.py` (`validate_from_admin_post`),
`conftest.py` (extend `validate`; add `validate_model_and_form`), `test_cron.py` (switch
to the model+form fixture).

**Why:** Complete the "one case table, parametrized over the enforcing subjects" model.
Subject 3 = the admin add-form HTTP POST. Rules the admin form enforces gain it with
zero new case data; cron (model+form only, no check) uses a `validate_model_and_form`
fixture.

- [ ] **Step 1: Extend the harness + RED** — in `conftest.py`: `validate` →
      `params=["check", "form", "model"]` (add `client`/`admin_user`, route `"form"` to
      `validate_from_admin_post`); add `validate_model_and_form` →
      `params=["form", "model"]`. Stub `validate_from_admin_post` in `utils.py`
      (`raise NotImplementedError`). Switch `test_cron.py` to take
      `validate_model_and_form` and call it (instead of `validate_from_model` directly).
      Run `uv run pytest tests/pg_cron/validators/test_cron.py -q` → FAIL (form subject
      unimplemented).
- [ ] **Step 2: Implement (prose)** —
      `validate_from_admin_post(client, admin_user, settings, **kwargs)`:
      `configure_pg_cron_backend(settings)`, `force_login(admin_user)`, build the POST
      payload from the `VALID` baseline + overrides (JSON-encode `args`/`kwargs`, blank
      empty optionals), POST `reverse("admin:django_absurd_pg_cron_scheduledtask_add")`;
      302 ⇒ None; else return the concatenated form-error text
      (`response.context["adminform"].form.errors`) so `assert MSG in result` holds with
      the same complete message. Rules the form can't express as free text (the `alias`
      charset — a choice field) stay model-only.
- [ ] **Step 3: GREEN** — `uv run pytest tests/pg_cron/validators/ -q` → PASS (cron
      asserts over model+form; name/task/queue/args/kwargs also assert at the admin-POST
      boundary).
- [ ] **Step 4: Commit**

```bash
git add tests/pg_cron/validators/utils.py tests/pg_cron/validators/conftest.py tests/pg_cron/validators/test_cron.py
git commit -m "test(pg_cron): admin-POST subject in the validator harness"
```

---

### Task 7: Documentation (AGENTS.md + docs site)

**Files:** Modify `django_absurd/AGENTS.md` (Scheduling), `docs/web/scheduling.md`,
`zensical.toml` (nav only if a new topic). Verify `uvx zensical build` → "No issues
found."

- [ ] **Step 1: AGENTS.md** — authoring `source="admin"` schedules in admin: editable
      fields, `alias`/`name` immutable on edit, DB-authoritative cron validation (incl.
      `<n> seconds` + high-frequency caveat), save/delete immediately (un)schedules the
      job, and that tearing down admin jobs is the guarded
      `absurd_sync_crons --teardown` (never automatic). `settings` rows stay read-only.
      Cross-link pg_cron scheduler + read-only admin sections.
- [ ] **Step 2: `docs/web/scheduling.md`** — mirror; add a `zensical.toml` nav entry
      only for a new top-level topic.
- [ ] **Step 3: Build** — `uvx zensical build` → "No issues found."
- [ ] **Step 4: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/scheduling.md zensical.toml
git commit -m "docs: admin-authored pg_cron schedules"
```

---

## Final verification (after all tasks)

- [ ] `uv run pytest tests/pg_cron` — full pg_cron suite green
      (`docker compose up -d db db_pg_cron`).
- [ ] `uv run pytest tests/core` + `tests/multidb` — no regressions.
- [ ] `uvx --with tox-uv tox` — full matrix + min-max mypy green.
- [ ] Patch coverage 100% on added/changed lines.
- [ ] `revdiff origin/main`; then adversarial review (Fable) end-of-branch.
- [ ] Ask before `gh pr create`; merge to main via PR only.

## Self-Review notes (spec coverage)

- Writable admin (source auto/hidden, alias Backend-choice + immutable, name immutable,
  editable set, settings read-only) → Task 5.
- Runtime job emission automatic on save/delete (signal, advisory lock, atomic, settings
  skip) → Task 3.
- Cron DB-authoritative validation folded into the harness (model+form) → Tasks 2 + 6;
  seconds caveat help text → Task 5.
- Cross-source clash (Phase A) + pk self-exclusion edit prerequisite → Task 1.
- Scheduler-switch orphan cleanup is now a guarded command (decision) — migrate never
  silently clears admin jobs → Task 4.
- Testing: subject 3 = admin POST → Task 6; all tests behavioral/HTTP/command, reusing
  suite infra; complete literal messages.
- Non-goals (edit settings rows, beat-in-admin, auto re-arm, DB-free matcher) —
  respected.
