# Admin-definable schedules — Phase A (validator extraction) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> or superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** extract the schedule-validation rules into shared pure validators, enforce
them model-first on `ScheduledTask` (`clean()` + field `validators=[…]`), rewire the
system checks to call them, and correct cron validation so croniter is beat-only and the
pg_cron grammar is DB-authoritative. No writable admin (Phase B).

**Architecture:** one `validators` module of pure functions raising `ValidationError`
(single source of rule truth). `ScheduledTask.clean()` + field `validators=[…]` invoke
them (model-first) → the admin form and any `full_clean()` caller get them free;
reconcile keeps using the system check (same validators). Checks call the validators
over settings `SCHEDULE` dicts, wrapping `ValidationError`→`E007`. Cron: beat=croniter
(unchanged), pg_cron=DB (settings at sync; check-time no longer croniter-validates
pg_cron; the removed 6-field rule).

**Tech Stack:** Django 6 system checks + model validation, pytest (function-based,
parametrized), croniter (beat only).

## Global Constraints

- Python 3.12+, Django 6.0+, psycopg v3.
- `import typing as t`; absolute imports only; functions contain a verb; no
  leading-underscore module-level helpers; helpers below their public callers.
- Validators are **pure functions raising `django.core.exceptions.ValidationError`** —
  the single source of rule truth; checks wrap them into `E007 CheckMessage`.
- **croniter is strictly the beat validator.** pg_cron cron grammar is validated by the
  DB (`cron.schedule`), never croniter.
- Behavior-preserving for existing checks EXCEPT: (a) pg_cron entries no longer
  croniter-checked at `check` time; (b) `check_pg_cron_cron_fields` removed.
- Tests: pytest function-based; assert literal `E007` message text inline (no importing
  the message constants); parametrized over subjects; full patch coverage on changed
  lines.
- `E007_MSG` today = `"django-absurd: invalid SCHEDULE entry."` — the shared validator
  messages must reproduce today's wording for the check path so existing assertions
  hold.

## File structure

- Create `django_absurd/pg_cron/validators.py` — pure validators (field-level +
  contextual). Lives in the pg_cron app because the pg_cron-specific rules (name/alias
  charset, jobname length, alias-is-pg_cron-backend, cross-source clash) belong with
  `ScheduledTask`; the generic ones (task, args/kwargs serializable, declared-queue,
  beat cron) are re-exported/shared from here too so there's one module.
- Modify `django_absurd/pg_cron/models.py` — `ScheduledTask.clean()` + field
  `validators=[…]`.
- Modify `django_absurd/checks.py` — `validate_schedule` calls shared validators; cron
  check scoped to beat.
- Modify `django_absurd/pg_cron/checks.py` — call shared validators; delete
  `check_pg_cron_cron_fields`.
- Modify `docs/WHY.md` — reverse the "No sub-minute on pg_cron" note.
- Create `tests/pg_cron/validators/` package — the parametrized-subject harness +
  per-rule case tables.
- Modify existing pg_cron check tests that assert 6-field-rejection at check time.
- Modify `CLAUDE.md` (testing conventions) — capture the methodology: one case table per
  rule, **parametrized over the real enforcing entrypoints** (`validate_<source>`
  subjects — check + `full_clean`), **model-first** validation, a plain `VALID` baseline
  dict so a single override isolates one rule; plus the two general rules — **assert the
  complete error message, never a fragment**, and **always alphabetize pytest
  parametrize values + fixture params**. Done as the last step of Task 1 (once the
  pattern is proven).

---

### Task 1: validators module + parametrized-subject harness (first rule: name charset)

Establishes the pattern the rest reuse: a pure validator, wired model-first, exercised
through two subjects (system check, `full_clean`) off one case table.

**Files:**

- Create: `django_absurd/pg_cron/validators.py`
- Modify: `django_absurd/pg_cron/models.py` (attach `validators=[validate_name_charset]`
  to `name`)
- Create: `tests/pg_cron/validators/__init__.py`, `tests/pg_cron/validators/conftest.py`
  (subject adapters), `tests/pg_cron/validators/test_name_charset.py`

**Interfaces:**

- Produces: `validate_name_charset(value: str) -> None` (raises `ValidationError` with
  the full message `"Schedule name must match [A-Za-z0-9_-]."`).
- Produces (test harness): two adapters `validate_model(**kwargs)` and
  `validate_check(settings, capsys, **kwargs)` — each merges `kwargs` over the `VALID`
  baseline, drives one real entrypoint, and returns the emitted error text (or `None`);
  plus a **parametrized `validate` fixture** (params `["check", "model"]`, alphabetized)
  that binds the needed fixtures and returns the matching adapter. Rules enforced on
  both paths parametrize via `validate`; model-only rules (cross-source clash,
  alias-is-backend) call `validate_model` directly. `validate_<source>` is the
  parametrize unit.

- [ ] **Step 1: Write the subject adapters + parametrized `validate` fixture
      (conftest)**

`tests/pg_cron/validators/conftest.py` — a plain `VALID` baseline dict (so a single
override isolates one rule), a model adapter (`full_clean`), a settings-`SCHEDULE` check
adapter, and the parametrized fixture. No baker, no leading-underscore helpers.

```python
import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from django_absurd.pg_cron.models import ScheduledTask

BACKEND = "django_absurd.backends.AbsurdBackend"

# Valid baseline: every field passes, so a single override isolates one rule.
VALID = dict(
    source="admin", alias="default", name="ok", task="tests.tasks.add",
    queue="", args=[], kwargs={}, cron="0 2 * * *", enabled=True,
)


def validate_model(**kwargs):
    """Subject: ScheduledTask.full_clean(). Return joined error text or None."""
    try:
        ScheduledTask(**{**VALID, **kwargs}).full_clean()
    except ValidationError as exc:
        return " ".join(m for msgs in exc.message_dict.values() for m in msgs)
    return None


def validate_check(settings, capsys, **kwargs):
    """Subject: the system check over a pg_cron SCHEDULE. Return captured E007 text."""
    fields = {**VALID, **kwargs}
    entry = {k: fields[k] for k in ("args", "cron", "kwargs", "queue", "task")}
    settings.TASKS = {"default": {"BACKEND": BACKEND, "OPTIONS": {
        "QUEUES": {"default": {}, "other": {}, "reports": {}},
        "SCHEDULER": "pg_cron", "SCHEDULE": {fields["name"]: entry}}}}
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


@pytest.fixture(params=["check", "model"])  # params alphabetized
def validate(request, settings, capsys):
    """Parametrized subject: same case run through each enforcing entrypoint."""
    if request.param == "check":
        return lambda **kwargs: validate_check(settings, capsys, **kwargs)
    return validate_model
```

- [ ] **Step 2: Write the failing case-table test (name charset)**

`tests/pg_cron/validators/test_name_charset.py` — one case table, run through **both**
subjects via the `validate` fixture (the check path validates the SCHEDULE key as the
name, the model path validates the `name` field).

```python
import pytest

pytestmark = pytest.mark.django_db(transaction=True)

NAME_MSG = "Schedule name must match [A-Za-z0-9_-]."
BAD = ["dot.dot", "has space", "unicodé", "with/slash"]  # alphabetized
GOOD = ["MixedCase123", "ok", "with-dash", "with_underscore"]  # alphabetized


@pytest.mark.parametrize("name", BAD)
def test_bad_name_rejected(validate, name):
    # assert the COMPLETE message, not a fragment
    assert NAME_MSG in (validate(name=name) or "")


@pytest.mark.parametrize("name", GOOD)
def test_good_name_accepted(validate, name):
    assert NAME_MSG not in (validate(name=name) or "")
```

- [ ] **Step 3: Run — verify it fails**

Run: `uv run pytest tests/pg_cron/validators/test_name_charset.py -q` Expected: FAIL —
`ScheduledTask` has no name-charset validation yet (bad names pass `full_clean`).

- [ ] **Step 4: Implement (prose)**

In `validators.py`: define `validate_name_charset` using Django's built-in
`RegexValidator` — Django already ships `validate_slug` for exactly this charset
(`[a-zA-Z0-9_-]+`, ASCII), so don't hand-roll `re`. Build a module-level
`validate_name_charset = RegexValidator(r"^[A-Za-z0-9_-]+\Z", message="Schedule name must match [A-Za-z0-9_-].")`
(the built-in validator class, our full domain message). In `models.py`, attach
`validators=[validate_name_charset]` to the `name` field. (Django runs field
`validators` during `full_clean`→`clean_fields`.)

- [ ] **Step 5: Run — verify pass**

Run: `uv run pytest tests/pg_cron/validators/test_name_charset.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/pg_cron/validators.py django_absurd/pg_cron/models.py tests/pg_cron/validators/
git commit -m "feat(pg_cron): validators module + name-charset validator (model-first)"
```

---

### Task 2: remaining field-level validators (alias charset, task, args, kwargs)

**Files:**

- Modify: `django_absurd/pg_cron/validators.py`, `django_absurd/pg_cron/models.py`
- Create: `tests/pg_cron/validators/test_alias_charset.py`, `test_task.py`,
  `test_args_kwargs_serializable.py`

**Interfaces:**

- Produces: `validate_alias_charset(value)`; `validate_task_path(value)` (importable +
  is-a-`Task`); `validate_json_serializable(value)`.

- [ ] **Step 1: Write failing tests (per rule, case tables)**

`test_task.py` (drive both subjects — task is enforced on the field AND reported by the
check):

```python
import pytest

pytestmark = pytest.mark.django_db(transaction=True)


# params alphabetized by path; assert the COMPLETE message (stable portion), not a fragment
@pytest.mark.parametrize("path,message", [
    ("os.getpid", "'os.getpid' is not a Django task."),
    ("tests.raises_on_import.anything",
     "task 'tests.raises_on_import.anything' could not be imported:"),
    ("tests.tasks.not_a_task",
     "task 'tests.tasks.not_a_task' could not be imported:"),
])
def test_bad_task_rejected(validate, path, message):
    # runs through both subjects (model full_clean + system check) via the fixture
    assert message in (validate(task=path) or "")
```

`test_args_kwargs_serializable.py` — a non-JSON value (e.g. a `set`) on `args`/`kwargs`
must be rejected with the full message `"args is not JSON-serializable."` /
`"kwargs is not JSON-serializable."`. `test_alias_charset.py` mirrors name-charset with
the full message `"Backend alias must match [A-Za-z0-9_-]."`. Both alphabetize their
parametrize values.

- [ ] **Step 2: Run — verify fail.** `uv run pytest tests/pg_cron/validators -q` → the
      new cases FAIL.

- [ ] **Step 3: Implement (prose)**

In `validators.py`: `validate_alias_charset` — a `RegexValidator` like the name one,
message `"Backend alias must match [A-Za-z0-9_-]."`; `validate_task_path` —
`import_string`, on any exception raise
`ValidationError("task {path!r} could not be imported: {exc!r}")`, and if the object
isn't a `django.tasks.Task` raise `"{path!r} is not a Django task."` (reuse today's core
wording so check assertions match); `validate_args_serializable` /
`validate_kwargs_serializable` — `json.dumps` in a try, raise the full
`"args is not JSON-serializable."` / `"kwargs is not JSON-serializable."` (one validator
per field so the message names the field — clearer than a shared closure). Attach to
`alias`, `task`, `args`, `kwargs` fields in `models.py`.

- [ ] **Step 4: Run — verify pass.** `uv run pytest tests/pg_cron/validators -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/validators.py django_absurd/pg_cron/models.py tests/pg_cron/validators/
git commit -m "feat(pg_cron): field validators — alias charset, task path, args/kwargs serializable"
```

---

### Task 3: contextual validators + `ScheduledTask.clean()` (jobname length, declared-queue, alias-is-pg_cron-backend)

These need 2+ fields or the backend config → live in `clean()`, resolving the backend
from `self.alias`.

**Files:**

- Modify: `django_absurd/pg_cron/validators.py`, `django_absurd/pg_cron/models.py`
- Create: `tests/pg_cron/validators/test_jobname_length.py`, `test_declared_queue.py`,
  `test_alias_is_pg_cron_backend.py`

**Interfaces:**

- Produces: `validate_jobname_length(alias, name)`;
  `validate_declared_queue(queue, task, declared_queues)`;
  `validate_alias_is_pg_cron_backend(alias)`. `ScheduledTask.clean()` resolves the
  backend by `self.alias` (via `get_absurd_backends()`), gathers declared queues, and
  calls these — collecting `ValidationError`s.

- [ ] **Step 1: Write failing tests**

`test_jobname_length.py` — a `name`/`alias` whose composed `absurd:admin:<alias>:<name>`
exceeds 63 bytes → `full_clean` raises with `"job name exceeds 63 bytes"`.
`test_declared_queue.py` — `queue="ghost"` (not declared) →
`"queue 'ghost' is not declared."`; and a task whose intrinsic `queue_name` isn't
declared with no override → same. `test_alias_is_pg_cron_backend.py` — `alias="nope"`
(no such pg_cron backend) → `"backend 'nope' is not a configured pg_cron backend."`
(both subjects: `full_clean` + check where the alias is the settings key — note the
settings subject can't have a bad alias, so this rule's subject is `full_clean` only;
document that in the test).

- [ ] **Step 2: Run — verify fail.**

- [ ] **Step 3: Implement (prose)**

Add the three contextual validators to `validators.py`. Implement
`ScheduledTask.clean()`: resolve `backend = get_absurd_backends().get(self.alias)`; if
absent → `validate_alias_is_pg_cron_backend` raises; else compute declared queues
(`get_declared_queues(backend)`) and the effective queue (existing `get_effective_queue`
logic — reuse/keep it), then call `validate_declared_queue` and
`validate_jobname_length`. Raise a single `ValidationError` mapping field→messages. Keep
`clean()` free of the cron savepoint-trial (that's Phase B); the cron field is validated
by the DB at sync for settings — for an admin row created in Phase A tests, `clean()`
does NOT yet DB-validate cron (Phase B adds it), so Phase A cron tests target the
check/beat path only.

- [ ] **Step 4: Run — verify pass.**

- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/validators.py django_absurd/pg_cron/models.py tests/pg_cron/validators/
git commit -m "feat(pg_cron): contextual validators + ScheduledTask.clean() (jobname, queue, alias-backend)"
```

---

### Task 4: cross-source `(alias,name)` clash validator

**Files:**

- Modify: `django_absurd/pg_cron/validators.py`, `django_absurd/pg_cron/models.py`
- Create: `tests/pg_cron/validators/test_cross_source_clash.py`

**Interfaces:**

- Produces: `validate_no_cross_source_clash(source, alias, name)` — queries
  `ScheduledTask` for a row with the SAME `(alias, name)` but the OTHER `source`; raises
  if found.

- [ ] **Step 1: Write failing test**

Seed a `source="settings"` row `(default, nightly)` (via `sync_crons` or direct create),
then `full_clean()` an admin row `(default, nightly)` → expect
`"a settings schedule 'nightly' already exists on backend 'default'."`. Reverse
direction (admin exists, settings row validated) covered symmetrically.

```python
import pytest
from django.core.exceptions import ValidationError

from django_absurd.pg_cron.models import ScheduledTask

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_row_clashing_with_settings_rejected():
    ScheduledTask.objects.create(source="settings", alias="default", name="nightly",
                                 task="tests.tasks.add", cron="0 2 * * *")
    row = ScheduledTask(source="admin", alias="default", name="nightly",
                        task="tests.tasks.add", cron="0 2 * * *")
    with pytest.raises(ValidationError) as exc:
        row.full_clean()
    assert "a settings schedule 'nightly' already exists" in str(exc.value)
```

- [ ] **Step 2: Run — verify fail.**

- [ ] **Step 3: Implement (prose)**

Add `validate_no_cross_source_clash` and call it from `clean()` (needs `self.pk`
exclusion so editing a row doesn't clash with itself). Message names the conflicting
source. Skip the check when the counterpart source is the SAME (normal unique_together
handles same-source).

- [ ] **Step 4: Run — verify pass. Step 5: Commit**
      (`feat(pg_cron): reject cross-source (alias,name) schedule clash`).

---

### Task 5: cron correction — croniter→beat only; pg_cron cron DB-deferred; remove 6-field rule; WHY.md

**Files:**

- Modify: `django_absurd/checks.py` (scope the cron croniter check to beat)
- Modify: `django_absurd/pg_cron/checks.py` (delete `check_pg_cron_cron_fields` + its
  call)
- Modify: `docs/WHY.md`
- Modify: existing tests asserting check-time 6-field rejection under pg_cron
  (`tests/pg_cron/test_pg_cron_checks.py`, `tests/pg_cron/test_scheduler_app_checks.py`)

**Interfaces:**

- Produces: a beat-only cron validator `validate_beat_cron(cron)` (croniter). pg_cron
  cron has no check-time validator (DB-authoritative).

- [ ] **Step 1: Write the failing/《changed》tests**

New: `tests/pg_cron/validators/test_cron.py` — under `SCHEDULER="pg_cron"`, a
`"30 seconds"` cron passes the CHECK (no E007 for cron) — asserts the check no longer
croniter-rejects it. Under `SCHEDULER="beat"`, `"30 seconds"` is rejected by croniter
(E007 invalid cron). Update the existing 6-field test: `"*/30 * * * * *"` under pg_cron
no longer yields the "6-field not supported" E007 at check time (that message is gone).

```python
import pytest

from tests.pg_cron.validators.conftest import validate_check

pytestmark = pytest.mark.django_db(transaction=True)


def test_pg_cron_interval_cron_passes_check(settings, capsys):
    out = validate_check(settings, capsys, cron="30 seconds")
    assert "absurd.E007" not in out  # pg_cron grammar deferred to the DB at sync
```

- [ ] **Step 2: Run — verify fail** (today croniter rejects `30 seconds` → E007
      present).

- [ ] **Step 3: Implement (prose)**

In `checks.py`: make `validate_schedule` cron-check scheduler-aware — only run the
croniter cron validator when the backend's scheduler is beat; for pg_cron, skip (grammar
validated by `cron.schedule` at sync). Thread the scheduler into `validate_schedule`
(its caller `check_absurd_schedule_config` knows `backend.scheduler`). In
`pg_cron/checks.py`: delete `check_pg_cron_cron_fields` and its call in
`validate_pg_cron_schedule`; keep name/alias + jobname + effective-queue (now delegating
to the shared validators). Remove the `E007_HINT_PG_CRON_SUBMINUTE` constant if now
unused.

- [ ] **Step 4: Update WHY.md (prose)**

Rewrite the "No sub-minute on pg_cron" subsection: pg_cron natively supports
`1–59 seconds` (distinct from the rejected croniter-6-field shim); pg_cron cron grammar
is DB-authoritative (validated by `cron.schedule` at sync, and — Phase B — by a
save-time savepoint-trial in the admin); croniter is the beat-only validator.

- [ ] **Step 5: Run — verify pass** (new cron tests green; the removed-6-field
      assertions updated/deleted). `uv run pytest tests/pg_cron -q`.

- [ ] **Step 6: Commit**
      (`fix(pg_cron): cron grammar is DB-authoritative; croniter beat-only; drop 6-field check`).

---

### Task 6: rewire checks to the shared validators (behavior-preserving) + full suite

**Files:**

- Modify: `django_absurd/checks.py`, `django_absurd/pg_cron/checks.py`

**Interfaces:**

- Consumes: all validators from Task 1–5.
- Produces: `checks.py`/`pg_cron/checks.py` calling the shared validators, wrapping
  `ValidationError`→`E007 CheckMessage`, no duplicated rule logic.

- [ ] **Step 1: (tests already exist)** the parametrized harness (subject 1 = system
      check) already asserts the check-path text for every rule. No new test; this task
      makes the check path delegate to the validators without changing emitted text.

- [ ] **Step 2: Implement (prose)**

Replace the inline rule bodies in `validate_schedule` / `validate_pg_cron_schedule` with
calls to the shared validators, converting each caught `ValidationError` into an
`Error(f"{E007_MSG} Schedule {name!r}: {ve.message}", hint=…, id="absurd.E007")`. Keep
the `E007_MSG` prefix + existing hints so the literal check text is unchanged.
Field-charset, jobname, queue, task, serializable rules all route through the one
module.

- [ ] **Step 3: Run — full pg_cron + core suites green.**

Run: `uv run pytest tests/pg_cron -q` then `uv run pytest tests/core -q`. Expected:
PASS; existing E007 assertions still match (message text preserved).

- [ ] **Step 4: Commit**
      (`refactor: system checks delegate to shared schedule validators`).

---

## Self-Review

- **Spec coverage:** validators pure + model-first (T1–T4); cron correction + WHY (T5);
  check rewire behavior-preserving (T6); parametrized harness subjects 1&2 (T1 conftest,
  used throughout). Cross-source clash (T4). Alias-is-pg_cron-backend (T3). All Phase-A
  spec items mapped. Phase B (writable admin, savepoint-trial cron in clean, signal
  emission, orphan teardown, admin-POST subject) explicitly deferred.
- **Placeholder scan:** none — each impl step is prose describing concrete edits; test
  steps carry real test code.
- **Naming:** verb-named validators (`validate_*`); `run`/subject adapters; no
  leading-underscore module helpers (regex constant is a plain name).
- **Open nuance carried into steps:** the `cron` rule has no `full_clean` subject in
  Phase A (savepoint-trial is Phase B) and no check-subject for pg_cron (DB-deferred) —
  its Phase-A coverage is the beat check path + the pg_cron "interval passes check"
  test.
