# Derive Scheduler from pg_cron App Presence — Implementation Plan (#68)

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `AbsurdBackend.scheduler` derives from whether `django_absurd.pg_cron` is in
`INSTALLED_APPS` instead of a user-set `OPTIONS["SCHEDULER"]` key. Drop the option, fold
away `absurd.E008` (the misconfig it caught is unrepresentable), and collapse every
now-tautological `scheduler != "pg_cron"` guard inside the `pg_cron` package.

**Architecture:** One derivation point (`AbsurdBackend.__init__` in `backends.py`).
Everything else is either dead code to delete (pg_cron-package guards that can only ever
see `scheduler == "pg_cron"` once that package's own modules only run when the app is
installed) or an unaffected reader (core-side `BEAT_DISABLED_UNDER_PG_CRON`, which still
needs the real conditional since core doesn't otherwise know install status).

**Tech Stack:** Django system checks framework, `django.apps.apps.is_installed`, pytest
(function-based, two suites: `tests/core` — pg_cron app absent — and `tests/pg_cron` —
app installed).

**Spec:** `docs/specs/2026-07-18-scheduler-from-app-presence-design.md` (read this first
— it has the full "what drops / what's kept / why" reasoning; this plan is the
step-by-step execution of it, plus two coverage-preservation gaps found while writing
the plan — see Task 2 and Task 9).

## Global Constraints

- pytest, function-based only, never class-based.
- No monkeypatching / `unittest.mock.patch` — drive real entrypoints (`call_command`,
  `client.post`, direct ORM) and assert observable behavior.
- Always alphabetize `@pytest.mark.parametrize` values and fixture params.
- Assert the COMPLETE error/message text, never a fragment.
- Hold 100% statement+branch coverage on every line this plan's diff adds or changes.
- No ruff ignores/`noqa` added without asking first.
- `import typing as t` always; never `from typing import X`. Absolute imports only.
- Comment hygiene: no comments restating code, no "no longer …" / "above already …"
  history comments.
- `docker compose up -d db db_pg_cron` must be running before any suite; use
  `uv run pytest tests/core` / `uv run pytest tests/pg_cron` (never a bare
  `uv run pytest` at repo root).

---

## Task 1: Derive `scheduler` from app presence (core mechanism)

**Files:**

- Modify: `django_absurd/backends.py` (`AbsurdBackend.__init__`, `AbsurdBackendOptions`
  TypedDict, top-of-file constant)
- Modify: `tests/pg_cron/test_scheduler_selector.py`
- Modify: `tests/core/test_scheduler_app_checks.py` (add the moved test — this file
  currently holds only E008 tests, which Task 2 deletes; adding here first keeps this
  task's own test green in isolation before Task 2 touches the file)

**Interfaces:**

- Produces: `AbsurdBackend.scheduler: str` — `"pg_cron"` when `django_absurd.pg_cron` is
  in `INSTALLED_APPS`, else `"beat"`. `PG_CRON_APP_NAME: str` constant, now defined in
  `backends.py` (moves here from `checks.py` in Task 2).

- [ ] **Step 1: Write the failing tests**

In `tests/pg_cron/test_scheduler_selector.py`, remove `test_scheduler_defaults_to_beat`
(it asserts `"beat"` while running in a suite where the app is always installed — false
post-change) and strip the now-dead `"SCHEDULER": "pg_cron"` line from its
`build_pg_cron_tasks` helper. Replace the whole file with:

```python
import re
import typing as t

import pytest
import pytest_django.fixtures
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends
from django_absurd.management.base import BEAT_DISABLED_UNDER_PG_CRON

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_pg_cron_tasks(
    schedule: dict[str, t.Any] | None = None,
) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULE": schedule or {},
            },
        }
    }


def test_scheduler_is_pg_cron_when_app_installed(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_pg_cron_tasks()
    assert get_absurd_backends()["default"].scheduler == "pg_cron"


def test_beat_command_refuses_under_pg_cron(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_pg_cron_tasks()
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_beat")


def test_worker_beat_flag_refuses_under_pg_cron(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_pg_cron_tasks()
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_worker", queue="default", beat=True)
```

Add the moved core-suite counterpart. `tests/core/test_scheduler_app_checks.py`
currently has no `django_absurd` import at all — add one, then append the test (this
whole file is deleted in Task 2 and the test moves again to
`tests/core/ test_checks.py`; for now just make Task 1 independently testable):

```python
from django_absurd.backends import get_absurd_backends
```

(add alongside the existing `import pytest_django.fixtures` /
`from django.core.management import call_command` imports.)

```python
def test_scheduler_defaults_to_beat(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": BASE_QUEUES}}
    }
    assert get_absurd_backends()["default"].scheduler == "beat"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_scheduler_selector.py -v --no-cov` Expected:
`test_scheduler_is_pg_cron_when_app_installed` FAILS — current code still reads
`OPTIONS.get("SCHEDULER", "beat")`, which is now absent from the TASKS dict, so
`scheduler` is `"beat"`, not `"pg_cron"`.

Run:
`uv run pytest tests/core/test_scheduler_app_checks.py::test_scheduler_defaults_to_beat -v --no-cov`
Expected: PASSES already (no code change needed for the "beat" default) — confirms the
added test is itself correct before Task 1's code change, so it stays a green witness
throughout.

- [ ] **Step 3: Implement the derivation**

In `django_absurd/backends.py`, add the import and move the constant. Near the top
(after existing imports, before `class TaskModel`), add:

```python
from django.apps import apps
```

(add to the existing import block, keeping imports sorted per ruff/isort — insert
alongside the other `django.*` imports).

Add the constant near the top of the file, above `class AbsurdBackendOptions`:

```python
PG_CRON_APP_NAME = "django_absurd.pg_cron"
```

Change `AbsurdBackendOptions` — remove the `SCHEDULER: str` line:

```python
class AbsurdBackendOptions(t.TypedDict, total=False):
    DATABASE: str
    DEFAULT_MAX_ATTEMPTS: int
    QUEUES: dict[str, CreateQueueOptions]
    ENABLE_ADMIN: bool
    ADMIN_SITE: tuple[str, ...]
    SCHEDULE: dict[str, dict[str, object]]
    CLEANUP: dict[str, str]
```

Change `AbsurdBackend.__init__`'s last line:

```python
        self.scheduler: str = (
            "pg_cron" if apps.is_installed(PG_CRON_APP_NAME) else "beat"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/pg_cron/test_scheduler_selector.py tests/core/test_scheduler_app_checks.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py tests/pg_cron/test_scheduler_selector.py tests/core/test_scheduler_app_checks.py
git commit -m "Derive AbsurdBackend.scheduler from pg_cron app presence"
```

---

## Task 2: Remove the SCHEDULER option's check surface (E007 unknown-value, E008)

**Files:**

- Modify: `django_absurd/checks.py`
- Modify: `django_absurd/backends.py` (delete `get_pg_cron_backends`)
- Modify: `tests/core/test_scheduler_app_checks.py` (full rewrite — delete, this file
  was E008-only plus Task 1's added test)
- Modify: `tests/pg_cron/test_scheduler_app_checks.py`
- Modify: `tests/pg_cron/test_pg_cron_checks.py`

**Interfaces:**

- Consumes: `PG_CRON_APP_NAME` from `django_absurd.backends` (moved there in Task 1).
- Produces: `check_pg_cron_app_ordering` (renamed from `check_scheduler_app_installed` —
  checks.py) — same W003 behavior, no more E008 branch.

- [ ] **Step 1: Write the failing test / delete the now-invalid ones**

Delete `tests/core/test_scheduler_app_checks.py` entirely — it existed only to prove
E008 fires (docstring:
`"""E008: SCHEDULER='pg_cron' requires pg_cron app — genuine absence in this suite."""`),
which is unrepresentable post-change; `test_scheduler_ defaults_to_beat` (added there in
Task 1) moves to `tests/core/test_checks.py` instead, alongside the other
backend-defaults assertions:

```bash
git rm tests/core/test_scheduler_app_checks.py
```

Add this to `tests/core/test_checks.py` (near its other backend/scheduler-adjacent tests
— anywhere in the file works, ordering isn't asserted):

```python
def test_scheduler_defaults_to_beat(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {"default": {}}}}
    }
    assert get_absurd_backends()["default"].scheduler == "beat"
```

`tests/core/test_checks.py` has no `from django_absurd...` import at all today (only
stdlib/Django/pytest imports plus its own `ABSURD` constant) — add a new import line
right after the `if t.TYPE_CHECKING:` block:

```python
from django_absurd.backends import get_absurd_backends
```

In `tests/pg_cron/test_scheduler_app_checks.py`:

- Rewrite `run_check` to drop the `scheduler` parameter entirely (it always resolves to
  `"pg_cron"` in this suite now, so it's not a variable worth passing):

```python
def run_check(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
    installed_apps: t.Sequence[str] | None = None,
    schedule: dict[str, t.Any] | None = None,
) -> str:
    if installed_apps is not None:
        settings.INSTALLED_APPS = installed_apps
    options: dict[str, t.Any] = {"QUEUES": BASE_QUEUES}
    if schedule is not None:
        options["SCHEDULE"] = schedule
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": options,
        }
    }
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err
```

- Delete `test_pg_cron_app_before_core_warns_under_beat` (its docstring's own premise —
  "a beat backend with the pg_cron app mis-ordered" — is unrepresentable; it's now a
  duplicate of `test_pg_cron_app_before_core_warns`).
- Update the two remaining call sites that passed `scheduler=`:
  `test_pg_cron_app_before_core_warns` calls
  `run_check(capsys, settings, build_apps_with_pg_cron_first(settings))` already (no
  `scheduler=` kwarg there — no change needed). `test_pg_cron_schedule_error_reported`
  and `test_pg_cron_app_config_path_before_core_warns` and
  `test_pg_cron_app_after_core_ clean` don't pass `scheduler=` either — confirm with a
  grep after editing:

```bash
grep -n "scheduler" tests/pg_cron/test_scheduler_app_checks.py
```

Expected: no output (all `scheduler=` kwargs were only on the two removed items).

In `tests/pg_cron/test_pg_cron_checks.py`:

- Delete `test_unknown_scheduler_value_rejected` (lines ~317-338, the "unknown SCHEDULER
  value" / typo-`"pgcron"` case — no longer representable, `SCHEDULER` isn't a settable
  key at all).
- Rewrite `run_pg_cron_check` to drop the `scheduler` options key:

```python
def run_pg_cron_check(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
    options: dict[str, t.Any],
) -> str:
    """Drive check with given queues/schedule and return output.

    options keys: queues, schedule.
    """
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": options["queues"],
                "SCHEDULE": options["schedule"],
            },
        }
    }
    try:
```

(the `try:`-onward body below that line is unchanged — only the `settings.TASKS =`
assignment and the docstring change.)

- Strip every `"scheduler": "pg_cron",` line from the 17 call-site option dicts (all
  byte-identical, 12-space indented):

```bash
sed -i '' '/^            "scheduler": "pg_cron",$/d' tests/pg_cron/test_pg_cron_checks.py
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`uv run pytest tests/core/test_checks.py::test_scheduler_defaults_to_beat tests/pg_cron/test_scheduler_app_checks.py tests/pg_cron/test_pg_cron_checks.py -v --no-cov`
Expected: `test_scheduler_defaults_to_beat` PASSES (no code change needed — same as Task
1). The `test_pg_cron_checks.py` / `test_scheduler_app_checks.py` tests should mostly
still PASS at this point too (their behavior doesn't depend on the E007/E008 removal,
only on the option key being gone, which Task 1 already made functionally irrelevant) —
this step is really about confirming the file edits are syntactically correct and
nothing regressed. If any FAIL, read the failure before proceeding to Step 3 — it likely
means a `scheduler=` reference was missed.

- [ ] **Step 3: Remove the E007/E008 check code**

In `django_absurd/checks.py`:

Change the import block:

```python
from django_absurd.backends import (
    PG_CRON_APP_NAME,
    get_absurd_backends,
    get_declared_queues,
)
```

Delete these constants entirely: `E007_HINT_SCHEDULER` (line 92), `E008_MSG` (94-97),
`E008_HINT` (98), `VALID_SCHEDULERS` (117), `PG_CRON_APP_NAME` (118 — moved to
backends.py in Task 1, delete the local definition here).

Rewrite `check_absurd_schedule_config` — drop the unknown-SCHEDULER branch:

```python
@register("absurd")
def check_absurd_schedule_config(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        scheduler = backend.scheduler
        declared_queues = set(get_declared_queues(backend))
        raw_schedule = backend.options.get("SCHEDULE", {})
        if not isinstance(raw_schedule, Mapping):
            errors.append(
                Error(
                    f'{E007_MSG} OPTIONS["SCHEDULE"] must be a mapping'
                    " of name -> spec.",
                    hint="Set SCHEDULE to a dict mapping schedule names to spec dicts.",
                    id="absurd.E007",
                )
            )
            continue
        for name, spec in raw_schedule.items():
            errors.extend(validate_schedule(name, spec, declared_queues, scheduler))
    return errors
```

Rename + rewrite `check_scheduler_app_installed` → `check_pg_cron_app_ordering`,
dropping the E008 branch:

```python
@register("absurd")
def check_pg_cron_app_ordering(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    if not apps.is_installed(PG_CRON_APP_NAME):
        return []

    # W003 tracks INSTALLED_APPS ordering: a mis-ordered app runs its post_migrate
    # cron reconcile before queue provisioning.
    app_names = resolve_installed_app_names()
    if (
        PG_CRON_APP_NAME in app_names
        and "django_absurd" in app_names
        and app_names.index(PG_CRON_APP_NAME) < app_names.index("django_absurd")
    ):
        return [DjangoWarning(W003_MSG, hint=W003_HINT, id="absurd.W003")]
    return []
```

In `django_absurd/backends.py`, delete `get_pg_cron_backends` entirely (its only caller
was the deleted E008 branch):

```python
def get_pg_cron_backends() -> dict[str, "AbsurdBackend"]:
    """The configured Absurd backends whose scheduler is pg_cron, keyed by alias."""
    return {
        alias: be
        for alias, be in get_absurd_backends().items()
        if be.scheduler == "pg_cron"
    }
```

Delete this whole function (nothing replaces it — `checks.py` no longer imports it).

- [ ] **Step 4: Run tests to verify they pass**

Run:
`uv run pytest tests/core/test_checks.py tests/pg_cron/test_scheduler_app_checks.py tests/pg_cron/test_pg_cron_checks.py -v --no-cov`
Expected: all PASS.

Run: `uv run pytest tests/core -v --no-cov` and
`uv run pytest tests/pg_cron -v --no-cov` Expected: no import errors (confirms nothing
else still imports `get_pg_cron_backends`, `E008_MSG`, `E008_HINT`, `VALID_SCHEDULERS`,
`E007_HINT_SCHEDULER`, or the old `check_scheduler_app_installed` name).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/checks.py django_absurd/backends.py tests/core/test_scheduler_app_checks.py tests/core/test_checks.py tests/pg_cron/test_scheduler_app_checks.py tests/pg_cron/test_pg_cron_checks.py
git commit -m "Drop absurd.E008 and the unknown-SCHEDULER E007 branch"
```

---

## Task 3: Collapse the dead scheduler guard in `pg_cron/checks.py`

**Files:**

- Modify: `django_absurd/pg_cron/checks.py`

**Interfaces:** none new — internal simplification only.

- [ ] **Step 1: Confirm existing tests already cover this without a scheduler guard**

No test in `tests/pg_cron/test_pg_cron_checks.py` ever set `scheduler="beat"` (Task 2's
grep confirmed 17 call sites, all `"pg_cron"`, plus the deleted typo case) — so there is
no test currently relying on this branch's `continue`. No new test is needed; the
existing `check_pg_cron_schedules` tests in that file continue to pass unchanged because
they never exercised the beat-skip path.

Run baseline: `uv run pytest tests/pg_cron/test_pg_cron_checks.py -v --no-cov` Expected:
all PASS (confirms the pre-change baseline before editing).

- [ ] **Step 2: Remove the dead branch**

In `django_absurd/pg_cron/checks.py`, change `check_pg_cron_schedules`:

```python
@register("absurd")
def check_pg_cron_schedules(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        declared_queues = set(get_declared_queues(backend))
        raw_schedule = backend.options.get("SCHEDULE", {})
        if not isinstance(raw_schedule, Mapping):
            continue  # core's check_absurd_schedule_config reports this
        for name, spec in raw_schedule.items():
            errors.extend(validate_pg_cron_schedule(name, spec, declared_queues))
    return errors
```

(only the `if backend.scheduler != "pg_cron": continue` line is removed.)

- [ ] **Step 3: Run tests to verify they still pass**

Run: `uv run pytest tests/pg_cron/test_pg_cron_checks.py -v --no-cov` Expected: all
PASS, identical results to Step 1.

- [ ] **Step 4: Commit**

```bash
git add django_absurd/pg_cron/checks.py
git commit -m "Collapse dead scheduler guard in check_pg_cron_schedules"
```

---

## Task 4: Collapse the dead scheduler guards in `pg_cron/models.py`

**Files:**

- Modify: `django_absurd/pg_cron/models.py`
- Modify: `tests/pg_cron/test_scheduledtask_model.py`

**Interfaces:**

- Produces: `resolve_pg_cron_backend` is DELETED — callers use `get_absurd_backend()`
  directly (it was a pure passthrough once the scheduler check collapses:
  `backend is None or backend.scheduler != "pg_cron"` → `backend is None`, so the whole
  function became `return get_absurd_backend()`).

- [ ] **Step 1: Write the failing test / delete the now-invalid one**

In `tests/pg_cron/test_scheduledtask_model.py`, delete
`test_full_clean_skips_backend_validation_when_not_pg_cron` (lines ~8-29) — its entire
premise is a `"SCHEDULER": "beat"` backend inside the pg_cron suite (app installed),
which is unrepresentable post-change; `ScheduledTask.clean()`'s collapsed condition
means validation ALWAYS runs once a backend resolves (which it always does in this
suite), so "skips validation" is no longer true.

Strip the now-dead `"SCHEDULER": "pg_cron"` line from
`test_scheduledtask_max_attempts_default_bubbles_from_backend` (~line 83):

```python
def test_scheduledtask_max_attempts_default_bubbles_from_backend(
    settings: SettingsWrapper,
) -> None:
    # the field default is the backend's DEFAULT_MAX_ATTEMPTS, not a hardcoded 5
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "DEFAULT_MAX_ATTEMPTS": 3,
            },
        }
    }
    task = ScheduledTask.objects.create(
        source="a",
        name="bubble",
        task="demo.tasks.ping",
        cron="* * * * *",
    )
    assert task.max_attempts == 3
```

- [ ] **Step 2: Run tests to verify the file still collects and passes**

Run: `uv run pytest tests/pg_cron/test_scheduledtask_model.py -v --no-cov` Expected: all
PASS (this step doesn't depend on the models.py collapse below — it's verifying the
test-file edit alone first).

- [ ] **Step 3: Collapse the three call sites**

In `django_absurd/pg_cron/models.py`:

`get_declared_queue_choices` (~line 50):

```python
def get_declared_queue_choices() -> list[tuple[str, str]]:
    """Declared queues for the configured Absurd backend, sorted, for use as field
    choices. Falls back to [("default", "default")] when no queues are declared.
    Called at form-render / validation / migration-state time — import-safe."""
    backend = get_absurd_backend()
    if backend is None:
        return [("default", "default")]
    queues = set(get_declared_queues(backend))
    if not queues:
        return [("default", "default")]
    return [(q, q) for q in sorted(queues)]
```

`ScheduledTask.clean` (~line 197):

```python
        backend = get_absurd_backend()
        if backend is not None:
            errors.update(self.validate_against_backend(backend))
```

(only this fragment changes — the rest of `clean()` is unchanged.)

`schedule_pg_cron_job` (~line 252) — replace the `resolve_pg_cron_backend()` call and
reword the docstring:

```python
    def schedule_pg_cron_job(self) -> None:
        """(Re)schedule this row's pg_cron job (``_dj:<source>:<name>``) and arm it to
        its enabled state. Called by the post_save signal for every write; a no-op when
        no Absurd backend is configured."""
        if get_absurd_backend() is None:
            return
```

(the rest of the method body is unchanged.)

`unschedule_pg_cron_job` (~line 274) — same treatment:

```python
    def unschedule_pg_cron_job(self) -> None:
        """Remove this row's pg_cron job, tolerating an already-gone job. Called by the
        post_delete signal for every deletion; a no-op when no Absurd backend is
        configured (symmetric with schedule_pg_cron_job) — so deletes don't error on a
        DB without one."""
        if get_absurd_backend() is None:
            return
```

Delete `resolve_pg_cron_backend` entirely (~lines 287-293):

```python
def resolve_pg_cron_backend() -> "AbsurdBackend | None":
    """The configured pg_cron backend, or None when the single backend is not a pg_cron
    backend — nothing to schedule for such a row."""
    backend = get_absurd_backend()
    if backend is None or backend.scheduler != "pg_cron":
        return None
    return backend
```

Delete this whole function.

Confirm `get_absurd_backend` is already imported at the top of the file (it is —
`from django_absurd.queues import get_absurd_backend, resolve_absurd_database`, line 23
— no import change needed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pg_cron -v --no-cov` Expected: all PASS — this touches
`clean()`, `schedule_pg_cron_job`, `unschedule_pg_cron_job`, and
`get_declared_queue_choices`, all exercised broadly across the pg_cron suite (admin
tests, schedule-emission tests, model tests), so run the whole suite here rather than a
narrow file.

Run: `grep -rn "resolve_pg_cron_backend" --include="*.py" .` from the repo root.
Expected: no output (confirms no stale references survived).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/models.py tests/pg_cron/test_scheduledtask_model.py
git commit -m "Collapse dead scheduler guards in pg_cron/models.py"
```

---

## Task 5: Collapse the dead teardown-on-switch branch in `pg_cron/apps.py`

**Files:**

- Modify: `django_absurd/pg_cron/apps.py`
- Modify: `tests/pg_cron/test_pg_cron_post_migrate.py`

**Interfaces:** none new — `reconcile_crons_after_migrate`'s signature is unchanged.

- [ ] **Step 1: Delete the now-invalid tests**

In `tests/pg_cron/test_pg_cron_post_migrate.py`, delete
`test_reconcile_tears_down_when_scheduler_switches_to_beat` (lines 168-183) and
`test_reconcile_emits_teardown_notice_when_backend_switches` (lines 338-354) — both
simulate "pg_cron app installed, backend switches to beat mid-test" via
`build_beat_tasks`, which is exactly the state this task's code change makes
unreachable. (`build_beat_tasks` itself is deleted in Task 7, once all its callers
across the suite are gone.)

- [ ] **Step 2: Run tests to verify the file still passes**

Run: `uv run pytest tests/pg_cron/test_pg_cron_post_migrate.py -v --no-cov` Expected:
all PASS (this file still imports `build_beat_tasks` for now — Task 7 removes the import
once every caller is gone; check after this deletion whether `build_beat_tasks` is still
referenced anywhere in this file):

```bash
grep -n "build_beat_tasks" tests/pg_cron/test_pg_cron_post_migrate.py
```

Expected: no output — both deleted tests were the only callers. If the import line
`from tests.pg_cron.utils import build_beat_tasks, build_pg_cron_tasks` now imports an
unused name, change it to `from tests.pg_cron.utils import build_pg_cron_tasks` (ruff
would flag the unused import otherwise).

- [ ] **Step 3: Collapse the branch**

In `django_absurd/pg_cron/apps.py`, rewrite `reconcile_crons_after_migrate`:

```python
def reconcile_crons_after_migrate(
    sender: AppConfig,
    *,
    verbosity: int = 1,
    stdout: t.TextIO | None = None,
    **kwargs: object,
) -> None:
    from django_absurd.pg_cron.reconcile import (  # noqa: PLC0415
        sync_admin_crons,
        sync_crons,
    )

    style = color_style()
    absurd_backends = get_absurd_backends()
    if not absurd_backends:
        return
    alias, backend = next(iter(absurd_backends.items()))
    try:
        created, pruned = sync_crons(backend)
        sync_admin_crons()
        lines = []
        if created:
            lines.append(f"  Scheduled {created}")
        if pruned:
            lines.append(f"  Pruned {pruned}")
        if lines and verbosity >= 1 and stdout is not None:
            stdout.write(
                style.MIGRATE_HEADING(f"Reconciling pg_cron schedules ({alias}):")
            )
            for line in lines:
                stdout.write(line)
    except (
        ImproperlyConfigured,
        OperationalError,
        ProgrammingError,
        InternalError,
        ImportError,
        TypeError,
        KeyError,
        AttributeError,
        ValueError,
    ):
        # Best-effort: migrate must never break. Skip this backend on an
        # unreachable DB, tables not yet present (faked/adopted migration, or
        # a multi-DB migrate firing post_migrate before the Absurd DB is
        # migrated), a bad dotted path in a schedule, a malformed SCHEDULE
        # spec, or an unserializable arg.
        logger.warning(
            "django-absurd: skipped cron reconcile for backend %r",
            alias,
            exc_info=True,
        )
```

(`teardown_crons` is no longer imported here — it stays defined in
`pg_cron/reconcile.py` and stays used by `absurd_sync_crons --teardown`, Task 6.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pg_cron/test_pg_cron_post_migrate.py -v --no-cov` Expected:
all PASS.

Run: `uv run pytest tests/pg_cron -v --no-cov` Expected: all PASS (this function is
exercised broadly via `migrate` in several other test files — confirm none of them
relied on the teardown-on-switch behavior).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/apps.py tests/pg_cron/test_pg_cron_post_migrate.py
git commit -m "Collapse dead teardown-on-scheduler-switch branch in reconcile_crons_after_migrate"
```

---

## Task 6: Collapse the dead scheduler-mismatch guard in `absurd_sync_crons`

**Files:**

- Modify: `django_absurd/pg_cron/management/commands/absurd_sync_crons.py`
- Modify: `tests/pg_cron/test_absurd_sync_crons_command.py`

**Interfaces:** none new — command's `--teardown`/`--noinput` flags unchanged.

- [ ] **Step 1: Delete the now-invalid tests**

In `tests/pg_cron/test_absurd_sync_crons_command.py`, delete
`test_sync_crons_command_refuses_when_scheduler_is_beat` (lines 61-68) — it asserts the
exact `CommandError` this task's code change removes — and
`test_teardown_allowed_when_scheduler_is_beat` (lines 110-123) — "teardown works even
under beat" is moot once beat-while-installed can't exist; teardown's
scheduler-independence remains covered by `test_teardown_removes_owned_cron_jobs` and
the other surviving teardown tests, none of which vary scheduler.

- [ ] **Step 2: Run tests to verify the file still passes and drop the dead import**

Run: `uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py -v --no-cov`
Expected: all PASS.

```bash
grep -n "build_beat_tasks" tests/pg_cron/test_absurd_sync_crons_command.py
```

Expected: no output. If the import line
`from tests.pg_cron.utils import build_beat_tasks, build_pg_cron_tasks` now imports an
unused name, change it to `from tests.pg_cron.utils import build_pg_cron_tasks`.

- [ ] **Step 3: Collapse the guard**

In `django_absurd/pg_cron/management/commands/absurd_sync_crons.py`, rewrite `handle`:

```python
    def handle(self, *args: t.Any, **options: t.Any) -> str | None:
        backend = resolve_backend()

        if options["teardown"]:
            if not options["no_input"] and not self.confirm_teardown(backend.alias):
                self.stdout.write("Aborted.")
                return None
            removed = teardown_crons(include_admin=True)
            self.stdout.write(
                f"Unscheduled all pg_cron jobs and removed {removed} schedule row(s) "
                f"— backend '{backend.alias}'."
            )
            return None

        try:
            created, pruned = sync_crons(backend)
            sync_admin_crons()
        except KeyError as exc:
            msg = (
                f"SCHEDULE entry is missing required key {exc} — "
                "run `manage.py check` for the E007 details."
            )
            raise CommandError(msg) from exc
        self.stdout.write(
            f"Synced {created} cron(s); pruned {pruned} — backend '{backend.alias}'."
        )
        return None
```

(only the `if backend.scheduler != "pg_cron": ...` block, which sat between the
`--teardown` early-return and the `try:`, is removed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pg_cron/test_absurd_sync_crons_command.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/management/commands/absurd_sync_crons.py tests/pg_cron/test_absurd_sync_crons_command.py
git commit -m "Collapse dead scheduler-mismatch guard in absurd_sync_crons"
```

---

## Task 7: Delete `build_beat_tasks`; sweep remaining SCHEDULER references

**Files:**

- Modify: `tests/pg_cron/utils.py`
- Modify: `tests/pg_cron/validators/utils.py`
- Modify: `tests/pg_cron/test_schedule_emission.py`
- Modify: `tests/pg_cron/test_pg_cron_sync_jobs.py`
- Modify: `tests/pg_cron/test_pg_cron_sync_rows.py`
- Modify: `tests/pg_cron/test_pg_cron_teardown.py`
- Modify: `tests/pg_cron/test_pg_cron_e2e.py`
- Modify: `tests/pg_cron/test_cleanup_schedule.py`
- Modify: `tests/pg_cron/test_admin/test_scheduledtask.py`

**Interfaces:** none new.

- [ ] **Step 1: Delete the now-invalid test and its helper**

In `tests/pg_cron/test_schedule_emission.py`, delete
`test_saving_non_pg_cron_backend_schedule_is_a_noop` (lines 81-94) — "a row whose
backend isn't pg_cron" is unrepresentable once the app being installed means
`resolve_absurd_backend()`-driven scheduling always applies. Update the import line:

```python
from tests.pg_cron.utils import build_pg_cron_tasks
```

Run: `grep -n "build_beat_tasks" tests/pg_cron/test_schedule_emission.py` — expect no
output before moving on.

Confirm no other caller of `build_beat_tasks` remains anywhere (Tasks 5 and 6 already
removed the other two):

```bash
grep -rn "build_beat_tasks" --include="*.py" tests/
```

Expected: only `tests/pg_cron/utils.py`'s own definition. If anything else appears, stop
and delete that caller first (it means an earlier task's cleanup was missed).

Delete `build_beat_tasks` from `tests/pg_cron/utils.py` and strip `SCHEDULER` from
`build_pg_cron_tasks`:

```python
"""Shared helpers for the pg_cron test suite (plain functions — fixtures live in
conftest.py; pg_cron catalog queries live on ``ScheduledTask.pg_cron``)."""

import typing as t

ABSURD_BACKEND: str = "django_absurd.backends.AbsurdBackend"
DECLARED_QUEUES: dict[str, dict[str, t.Any]] = {
    "default": {},
    "other": {},
    "reports": {},
}


def build_pg_cron_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD_BACKEND,
            "OPTIONS": {
                "QUEUES": DECLARED_QUEUES,
                "SCHEDULE": schedule,
            },
        }
    }
```

- [ ] **Step 2: Strip the remaining inline `SCHEDULER` keys**

Each of these has exactly one `"SCHEDULER": "pg_cron",` line (confirmed by grep) except
`test_cleanup_schedule.py`, which has three. Remove each line:

```bash
sed -i '' '/^                "SCHEDULER": "pg_cron",$/d' tests/pg_cron/test_pg_cron_sync_jobs.py
sed -i '' '/^                "SCHEDULER": "pg_cron",$/d' tests/pg_cron/test_pg_cron_sync_rows.py
sed -i '' '/^                "SCHEDULER": "pg_cron",$/d' tests/pg_cron/test_pg_cron_teardown.py
sed -i '' '/^            "SCHEDULER": "pg_cron",$/d' tests/pg_cron/test_pg_cron_e2e.py
sed -i '' '/^                "SCHEDULER": "pg_cron",$/d' tests/pg_cron/test_cleanup_schedule.py
```

In `tests/pg_cron/validators/utils.py`, strip the line from `configure_pg_cron_backend`:

```python
def configure_pg_cron_backend(
    settings: SettingsWrapper,
    schedule: dict[str, t.Any] | None = None,
) -> None:
    """A pg_cron 'default' backend so model clean() resolves it (declared queues),
    and the check has a SCHEDULE to validate."""
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {
                "QUEUES": QUEUES,
                "SCHEDULE": schedule or {},
            },
        }
    }
```

In `tests/pg_cron/test_admin/test_scheduledtask.py`, strip all three occurrences (the
module-level `TASKS` dict at line 29, the inline `narrow_to_default_queue_only` dict at
line 200, and the inline dict in `test_create_and_sync_produce_identical_spawn_columns`
at line 328):

```bash
sed -i '' '/^            "SCHEDULER": "pg_cron",$/d' tests/pg_cron/test_admin/test_scheduledtask.py
sed -i '' 's/"OPTIONS": {"QUEUES": {"default": {}}, "SCHEDULER": "pg_cron"}/"OPTIONS": {"QUEUES": {"default": {}}}/' tests/pg_cron/test_admin/test_scheduledtask.py
```

(the first `sed` handles the two multi-line dict occurrences at lines 29 and 328; the
second handles the single-line dict at line 200 — run both, order doesn't matter.)

- [ ] **Step 3: Verify the sweep is complete**

```bash
grep -rln "SCHEDULER" --include="*.py" tests/ django_absurd/
```

Expected output: only files that legitimately still mention `SCHEDULER` as prose (not a
settable key) — at this point that should be none in `tests/`, and none in
`django_absurd/` either (Tasks 1-6 already removed every code reference). If anything
unexpected appears, read it before proceeding — it's either a doc-string mentioning the
old option name (fine to leave for Task 10's docs pass, but flag it here) or a missed
test.

- [ ] **Step 4: Run the full pg_cron suite**

Run: `uv run pytest tests/pg_cron -v --no-cov` Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/pg_cron/utils.py tests/pg_cron/validators/utils.py tests/pg_cron/test_schedule_emission.py tests/pg_cron/test_pg_cron_sync_jobs.py tests/pg_cron/test_pg_cron_sync_rows.py tests/pg_cron/test_pg_cron_teardown.py tests/pg_cron/test_pg_cron_e2e.py tests/pg_cron/test_cleanup_schedule.py tests/pg_cron/test_admin/test_scheduledtask.py
git commit -m "Delete build_beat_tasks; strip remaining inline SCHEDULER keys"
```

---

## Task 8: Reword `BEAT_DISABLED_UNDER_PG_CRON`

**Files:**

- Modify: `django_absurd/management/base.py`

**Interfaces:** `BEAT_DISABLED_UNDER_PG_CRON: str` — same name, new text; consumed by
`absurd_beat.py` and `absurd_worker.py` unchanged, and by
`tests/pg_cron/test_scheduler_selector.py` via `re.escape(BEAT_DISABLED_UNDER_PG_CRON)`
(Task 1 — no test text is duplicated, so no test edit needed here).

- [ ] **Step 1: Confirm the baseline**

Run: `uv run pytest tests/pg_cron/test_scheduler_selector.py -v --no-cov` Expected: all
PASS (both tests referencing this message use `re.escape(...)` against the live
constant, so they'll auto-adapt to the reworded text — this step just confirms they pass
before the edit, for comparison after).

- [ ] **Step 2: Reword the message**

In `django_absurd/management/base.py`:

```python
BEAT_DISABLED_UNDER_PG_CRON = (
    "the pg_cron app is installed: schedules run in the database via pg_cron,"
    " so the beat process is disabled."
    " Reconcile the pg_cron jobs with 'manage.py absurd_sync_crons'"
    " (migrate does it too)."
)
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/pg_cron/test_scheduler_selector.py -v --no-cov` Expected: all
PASS (identical result to Step 1 — confirms the `re.escape` match tracked the new text
automatically).

- [ ] **Step 4: Commit**

```bash
git add django_absurd/management/base.py
git commit -m "Reword BEAT_DISABLED_UNDER_PG_CRON — SCHEDULER isn't a setting anymore"
```

---

## Task 9: Preserve `is_valid_cleanup`'s pg_cron-branch coverage

**Files:**

- Modify: `tests/core/test_checks.py`
- Modify: `tests/pg_cron/test_pg_cron_checks.py`

**Context (why this task exists — not in the original spec):**
`check_absurd_ cleanup_config` calls `is_valid_cleanup(cleanup, backend.scheduler)`
(`django_absurd/checks.py`), which branches on `scheduler == "beat"` vs pg_cron. Its
only test coverage today is `tests/core/test_checks.py::test_invalid_cleanup_errors` /
`test_valid_cleanup_no_error`, both parametrized over `scheduler=["beat", "pg_cron"]`
with an explicit `"SCHEDULER": scheduler` in `OPTIONS`. In the CORE suite the pg_cron
app is never installed, so post-change every backend there resolves to
`scheduler="beat"` regardless of what `OPTIONS` says — the `"pg_cron"` parametrize id
would silently exercise the **beat** branch instead of the pg_cron one, and the pg_cron
branch would end up with **zero** test coverage. Move that coverage to the pg_cron
suite, where the app is genuinely installed.

**Interfaces:** none new — `is_valid_cleanup`'s signature and logic are unchanged (this
is test-only rebalancing).

- [ ] **Step 1: Write the new pg_cron-suite tests (RED first — they exercise real,
      already-correct production code, so "RED" here means "does not exist yet",
      confirmed by a collection check)**

In `tests/pg_cron/test_pg_cron_checks.py`, add a small helper and two tests (anywhere in
the file — e.g. near the top, after `BASE_QUEUES`):

```python
def run_pg_cron_cleanup_check(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
    cleanup: dict[str, t.Any],
) -> str:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": BASE_QUEUES, "CLEANUP": cleanup},
        }
    }
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_pg_cron_cleanup_accepts_arbitrary_nonempty_schedule(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under pg_cron, CLEANUP's cron grammar is DB-authoritative at sync time — the
    check only requires a non-empty string, unlike beat's croniter validation."""
    out = run_pg_cron_cleanup_check(
        settings, capsys, {"schedule": "not a cron but pg_cron doesn't validate this"}
    )
    assert "absurd.E010" not in out


def test_pg_cron_cleanup_rejects_empty_schedule(
    settings: pytest_django.fixtures.SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = run_pg_cron_cleanup_check(settings, capsys, {"schedule": ""})
    assert "absurd.E010" in out
```

- [ ] **Step 2: Run the new tests to verify they pass**

Run:
`uv run pytest "tests/pg_cron/test_pg_cron_checks.py::test_pg_cron_cleanup_accepts_arbitrary_nonempty_schedule" "tests/pg_cron/test_pg_cron_checks.py::test_pg_cron_cleanup_rejects_empty_schedule" -v --no-cov`
Expected: both PASS immediately — `is_valid_cleanup`'s pg_cron branch already exists and
is correct; this task adds coverage, it doesn't change behavior. (If either FAILS,
that's a genuine pre-existing bug in `is_valid_cleanup` surfacing for the first time —
stop and investigate rather than adjusting the assertion to match.)

- [ ] **Step 3: Remove the now-misleading pg_cron parametrize cases from the core
      suite**

In `tests/core/test_checks.py`, rewrite both functions to drop the `scheduler`
parametrize dimension (core suite backends are always `"beat"` — the `"pg_cron"` id
would silently test the same beat code path a second time, which is worse than no test:
it reads as pg_cron coverage that isn't there):

```python
@pytest.mark.parametrize(
    "cleanup",
    [
        "0 2 * * *",
        {"schedule": ""},
        {"schedule": "0 2 * * *", "unknown": 1},
        {"schedule": "not a cron"},
        {"schedule": 5},
    ],
)
def test_invalid_cleanup_errors(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
    cleanup: str | dict[str, t.Any],
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "CLEANUP": cleanup,
            },
        }
    }
    out = run_absurd_check(capsys)
    assert E010_MSG in out
    assert E010_HINT in out
    assert "absurd.E010" in out


def test_valid_cleanup_no_error(
    settings: "pytest_django.fixtures.SettingsWrapper",
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "CLEANUP": {"schedule": "0 2 * * *"},
            },
        }
    }
    out = run_absurd_check(capsys)
    assert "absurd.E010" not in out
```

- [ ] **Step 4: Run tests to verify everything passes**

Run: `uv run pytest tests/core/test_checks.py -v --no-cov` Expected: all PASS.

Run: `uv run pytest tests/pg_cron/test_pg_cron_checks.py -v --no-cov` Expected: all
PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/core/test_checks.py tests/pg_cron/test_pg_cron_checks.py
git commit -m "Move is_valid_cleanup pg_cron-branch coverage to the pg_cron suite"
```

---

## Task 10: Docs + examples sweep

**Files:**

- Modify: `docs/web/cron-jobs.md`
- Modify: `docs/web/configuration.md`
- Modify: `django_absurd/AGENTS.md`
- Modify: `examples/beat/app.py`
- Modify: `examples/pg_cron/app.py`

**Interfaces:** none — documentation and example-app config only.

- [ ] **Step 1: Update the example apps**

In `examples/beat/app.py`, remove the `"SCHEDULER": "beat"` line from the `OPTIONS` dict
(scheduling is now implicit — this example's `EXTRA_APPS` doesn't include
`django_absurd.pg_cron`, so it already derives `"beat"`):

```python
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULE": {"tick": {"task": "app.tick", "cron": "* * * * *"}},
            },
        }
    },
```

In `examples/pg_cron/app.py`, remove the `"SCHEDULER": "pg_cron"` line (this example's
`EXTRA_APPS` includes `"django_absurd.pg_cron"`, so it derives `"pg_cron"`):

```python
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULE": {"ping": {"task": "app.ping", "cron": "* * * * *"}},
            },
        }
    },
```

- [ ] **Step 2: Update `docs/web/configuration.md`**

Delete the `absurd.E008` row from the check-messages table:

```
| `absurd.E008` | `SCHEDULER` is `pg_cron` but `django_absurd.pg_cron` is not in `INSTALLED_APPS` (see [Cron Jobs](cron-jobs.md)). |
```

Delete this row entirely — no replacement row (the misconfig is unrepresentable).

- [ ] **Step 3: Update `docs/web/cron-jobs.md`**

Change the "Database-side: pg_cron" section intro (currently
`With SCHEDULER="pg_cron", Postgres fires the schedule directly`):

```markdown
## Database-side: pg_cron

Installing the `django_absurd.pg_cron` app makes Postgres fire the schedule directly —
no beat process to run. django-absurd materialises each declared schedule into a
[pg_cron](https://github.com/citusdata/pg_cron) job; your existing
[workers](how-it-works.md#workers) pick up and run the tasks as usual.
```

Change step 2 of "Get running" (currently `Point the backend at the pg_cron scheduler:`
with a `"SCHEDULER": "pg_cron",` line in the example) — merge into step 1 since there's
no separate "point the backend" step anymore, renumber accordingly:

````markdown
**1. Add the opt-in app to `INSTALLED_APPS`, after `"django_absurd"`:**

```python title="settings.py"
INSTALLED_APPS = [
    # ...
    "django_absurd",
    "django_absurd.pg_cron",   # must come after "django_absurd" — scheduling becomes
                                # pg_cron the moment this app is installed
]
```
````

This app owns the projection table + wrapper-function migrations and reconciles your
`SCHEDULE` on `post_migrate`. Its first migration runs
`CREATE EXTENSION IF NOT EXISTS pg_cron`: **if the extension isn't installed yet, we
create it**; if it's already there (managed Postgres, or a superuser installed it) that
step is a no-op and needs no special rights.

**2. Declare your `SCHEDULE`:**

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.send_report",
                    "cron": "0 2 * * *",
                },
                "heartbeat": {
                    "task": "myapp.tasks.ping",
                    "cron": "*/5 * * * *",
                    "queue": "monitoring",           # optional; must be a declared queue
                    "kwargs": {"source": "pg_cron"}, # kwargs passed to the task; optional
                },
            },
        },
    },
}
```

````

Update the `manage.py check` paragraph that mentions E008 — currently:

```markdown
Run `manage.py check` to catch misconfiguration early: `absurd.E008` if
`SCHEDULER="pg_cron"` but `"django_absurd.pg_cron"` is missing from `INSTALLED_APPS`;
`absurd.W003` if the app is ordered before `"django_absurd"`.
````

replace with:

```markdown
Run `manage.py check` to catch misconfiguration early: `absurd.W003` if the app is
ordered before `"django_absurd"`.
```

Update "Beat and pg_cron are mutually exclusive per backend" — currently:

```markdown
**Beat and pg_cron are mutually exclusive per backend.** Setting `SCHEDULER="pg_cron"`
and running `absurd_beat` (or `absurd_worker --beat`) against the same backend raises a
`CommandError` — use one or the other.
```

replace with:

```markdown
**Beat and pg_cron are mutually exclusive.** Running `absurd_beat` (or
`absurd_worker --beat`) while `django_absurd.pg_cron` is installed raises a
`CommandError` — install the app, or run beat, not both.
```

Update "Cron grammar is pg_cron's own" — currently starts
`Under SCHEDULER="pg_cron" an expression is either a`; replace with:

```markdown
**Cron grammar is pg_cron's own.** Once `django_absurd.pg_cron` is installed, an
expression is either a
```

Update the command error description in "Reconcile explicitly" — currently:

```markdown
The command is loud: it reports synced/pruned counts, and fails with a non-zero exit on
error — a wrong `SCHEDULER` or a malformed `SCHEDULE` entry (missing `task`/`cron`)
raises `CommandError`, while a missing extension or insufficient privilege surfaces as
the underlying database error.
```

replace with:

```markdown
The command is loud: it reports synced/pruned counts, and fails with a non-zero exit on
error — a malformed `SCHEDULE` entry (missing `task`/`cron`) raises `CommandError`,
while a missing extension or insufficient privilege surfaces as the underlying database
error.
```

Add the teardown-before-uninstall note (new paragraph, end of "Reconcile explicitly"
section):

```markdown
**Uninstalling pg_cron.** If you remove `"django_absurd.pg_cron"` from `INSTALLED_APPS`,
its `post_migrate` reconcile no longer runs, so nothing tears down existing jobs
automatically. Run `manage.py absurd_sync_crons --teardown --noinput` **before**
removing the app — not after — so it can still see and remove them.
```

- [ ] **Step 4: Update `django_absurd/AGENTS.md`**

Change the "Scheduling recurring tasks" intro — currently:

```markdown
django-absurd supports two schedulers, selected per backend with `OPTIONS["SCHEDULER"]`:

| Value       | Default | Description                                                      |
| ----------- | ------- | ---------------------------------------------------------------- |
| `"beat"`    | yes     | In-process beat; evaluates cron and enqueues via the normal path |
| `"pg_cron"` | no      | Database-side; Postgres fires jobs directly via `pg_cron`        |
```

replace with:

```markdown
django-absurd supports two schedulers, selected by whether `"django_absurd.pg_cron"` is
in `INSTALLED_APPS`:

| State                               | Scheduler   | Description                                                      |
| ----------------------------------- | ----------- | ---------------------------------------------------------------- |
| app absent (default)                | `"beat"`    | In-process beat; evaluates cron and enqueues via the normal path |
| `"django_absurd.pg_cron"` installed | `"pg_cron"` | Database-side; Postgres fires jobs directly via `pg_cron`        |
```

Update the "Declare schedules" example — remove the commented-out
`# "SCHEDULER": "beat",   # default; omit for beat` line entirely.

Update the "pg_cron backend" section intro — currently
`Set SCHEDULER = "pg_cron" to let Postgres fire schedules directly`; replace with:

```markdown
Install `"django_absurd.pg_cron"` to let Postgres fire schedules directly — no beat
process needed.
```

Update the "Enabling" paragraph — currently:

```markdown
Add `"django_absurd.pg_cron"` to `INSTALLED_APPS` **after** `"django_absurd"` — the
opt-in app owns the projection table and wrapper function migrations and reconciles the
`SCHEDULE` on `post_migrate`. Running `manage.py check` reports `absurd.E008` if
`SCHEDULER="pg_cron"` is set but the app is absent, and `absurd.W003` if the app is
present but ordered before `"django_absurd"`.
```

replace with:

```markdown
Add `"django_absurd.pg_cron"` to `INSTALLED_APPS` **after** `"django_absurd"` — the
opt-in app owns the projection table and wrapper function migrations, switches the
backend's scheduler to `"pg_cron"`, and reconciles the `SCHEDULE` on `post_migrate`.
Running `manage.py check` reports `absurd.W003` if the app is present but ordered before
`"django_absurd"`.
```

Remove `"SCHEDULER": "pg_cron",` from the `OPTIONS` code block that follows.

Update the "Beat and pg_cron are mutually exclusive" line — currently:

```markdown
Beat and pg_cron are **mutually exclusive** per backend: running `absurd_beat` or
`absurd_worker --beat` against a backend with `SCHEDULER="pg_cron"` raises
`CommandError`.
```

replace with:

```markdown
Beat and pg_cron are **mutually exclusive**: running `absurd_beat` or
`absurd_worker --beat` while `django_absurd.pg_cron` is installed raises `CommandError`.
```

Remove the `- an unknown SCHEDULER value` bullet from the E007 misconfiguration list
(~line 458).

Remove the `absurd.E008` bullet from wherever it's listed alongside other check IDs in
this file (search `grep -n "E008" django_absurd/AGENTS.md` to find any remaining
mentions beyond the ones already addressed above, and delete them).

- [ ] **Step 5: Verify the doc sweep is complete**

```bash
grep -rn "SCHEDULER\|E008" docs/web/ django_absurd/AGENTS.md examples/
```

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add docs/web/cron-jobs.md docs/web/configuration.md django_absurd/AGENTS.md examples/beat/app.py examples/pg_cron/app.py
git commit -m "docs: derive scheduler from pg_cron app presence, drop SCHEDULER/E008"
```

---

## Task 11: Full verification + WHY.md sync

**Files:** none new — verification + a docs-sync skill invocation.

- [ ] **Step 1: Full suite run**

Ensure both DB services are up (`docker compose up -d db db_pg_cron` if not already
running — check `docker ps` first), then:

```bash
uv run pytest tests/core -v
uv run pytest tests/pg_cron -v
uv run pytest tests/multidb -v
```

Expected: all PASS across all three suites (no `--no-cov` this time — let coverage run
so the `--cov-report=term` output can be eyeballed for anything under 100% on lines this
plan's diff touched).

- [ ] **Step 2: Type + lint check**

```bash
uv run mypy django_absurd tests
uvx ruff check .
uvx ruff format --check .
```

Expected: no errors. If mypy flags the deleted `resolve_pg_cron_backend` or
`get_pg_cron_backends` names anywhere, that's a missed caller — grep and fix before
proceeding.

- [ ] **Step 3: Confirm no stray references**

```bash
grep -rln "SCHEDULER\|E008\|get_pg_cron_backends\|resolve_pg_cron_backend\|check_scheduler_app_installed" --include="*.py" --include="*.md" .
```

Expected: no output (or only this plan/spec file itself and `docs/HISTORY.md` if it
already existed before this change — both are fine to mention the old names
historically).

- [ ] **Step 4: Sync WHY.md**

Run the `sync-docs` skill (or `/dream`) to fold this decision into `docs/WHY.md` — the
durable "why" behind deriving scheduler from app presence, superseding the old
per-backend `SCHEDULER` knob. This step has no test — it's a documentation-generation
step, follow the skill's own process.

- [ ] **Step 5: Final commit (if WHY.md changed)**

```bash
git add docs/WHY.md
git commit -m "docs: sync WHY.md for scheduler-from-app-presence"
```
