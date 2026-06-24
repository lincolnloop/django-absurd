# Tasks-API Queue Config Migration (SP1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move django-absurd queue config off the `ABSURD_QUEUES`/`ABSURD_DATABASE`
settings onto a `TASKS` `AbsurdBackend` alias (Django Tasks framework), re-sourcing the
sync command, system checks, and DB router from it.

**Architecture:** A new `AbsurdBackend(BaseTaskBackend)` carries config in its `TASKS`
alias dict. `queues.py` readers resolve config from the backend instead of top-level
settings. Checks split into a config check (always-on) and a DB-state check (gated by
the `databases` arg via `Tags.database`). No task execution yet — `enqueue`/`get_result`
raise `NotImplementedError` (SP2); the worker is SP3.

**Tech Stack:** Django 6.0 `django.tasks`, absurd-sdk, psycopg3, pytest + pytest-django,
tox-uv.

## Global Constraints

- Python floor **3.12**; Django floor **6.0** (drop 5.2). `requires-python = ">=3.12"`;
  `[project] dependencies` `Django>=6.0`; ruff `target-version = "py312"`.
- tox `env_list`: `py3{12,13,14}-django60`, `latest`, `py312-django60-mypy`,
  `py314-django60-mypy`. Floor `py312-django60` resolves `lowest-direct`; rest
  `highest`.
- Config is **either/or**: top-level `QUEUES` (list of names) XOR `OPTIONS["QUEUES"]`
  (dict name→policy). Never both.
- Backend resolution is **`isinstance(be, AbsurdBackend)`**, never class identity
  (subclasses count).
- Per-queue policy keys = absurd-sdk `CreateQueueOptions`; `storage_mode` is
  create-only.
- Imports: `import typing as t` (never `from typing import X`); absolute imports only.
- Functions contain a verb; no leading-underscore module helpers/constants; helpers
  placed BELOW their public callers.
- Check `msg` states the PROBLEM; `hint` states the RESOLUTION — never duplicate fix
  text in both.
- pytest function-based only; autouse `_enable_db(db)` gives DB access — add
  `@pytest.mark.django_db(transaction=True)` only when commits/DDL needed,
  `databases=[...]` for multi-DB. No mocks/`patch`. Test commands & checks by RUNNING
  them (`call_command`), capture with `capsys`, assert full emitted message text.
- Tests drive config via the pytest-django `settings` fixture, reassigning
  `settings.TASKS` **wholesale** — the assignment fires `setting_changed`, which resets
  the cached `task_backends`. Nested-key mutation does NOT fire the signal, so never
  mutate `settings.TASKS[...]` in place.

---

### Task 1: Version bump to Django 6.0 / Python 3.12

**Files:**

- Modify: `pyproject.toml` (`requires-python`, `Django` dep,
  `[tool.ruff] target-version`)
- Modify: `tox.ini` (`env_list`, `deps`, `uv_resolution`)
- Modify: `.github/workflows/ci.yml` (matrix)
- Modify: `CLAUDE.md` (Runtime note: floor Django 6.0 / Python 3.12)

**Interfaces:**

- Produces: a repo whose floor env is `py312-django60`; `import django.tasks` available
  in every test env.

This task is infrastructure (build/matrix config), so it is verified by running tooling
rather than a RED→GREEN pytest cycle.

- [ ] **Step 1: Write the floor-guard test**

Add to `tests/test_packaging.py`:

```python
def test_django_tasks_available_on_floor():
    import django

    assert django.VERSION[:2] >= (6, 0)
    import django.tasks  # noqa: F401 — must import on the supported floor
```

- [ ] **Step 2: Run it (documents the floor; green on 6.0)**

Run: `uv run pytest tests/test_packaging.py::test_django_tasks_available_on_floor -v`
Expected: PASS on the current Django 6.0 env (guards against a future floor regression).

- [ ] **Step 3: Apply the version-bump edits (prose)**

- `pyproject.toml`: set `requires-python = ">=3.12"`; change the `Django>=5.2`
  dependency to `Django>=6.0`; set `[tool.ruff] target-version = "py312"`.
- `tox.ini`: replace `env_list` with `py3{12,13,14}-django60`, `latest`,
  `py312-django60-mypy`, `py314-django60-mypy`. Remove the `django52` line from `deps`;
  keep `django60: django>=6.0,<6.1`. Set `uv_resolution` keyed on the **Python** factor
  (non-overlapping — avoids two conditionals matching the same env):
  `py312: lowest-direct` (floor pins lowest-compatible deps) and `py3{13,14}: highest`.
  (`latest` is a separate `[testenv:latest]` using the lock runner — no
  `uv_resolution`.)
- `.github/workflows/ci.yml`: set the matrix `env` list to
  `[py312-django60, py313-django60, py314-django60, latest, py312-django60-mypy, py314-django60-mypy]`.
- `CLAUDE.md`: update the Runtime section to state the floor is Django 6.0 / Python
  3.12.

- [ ] **Step 4: Verify tooling**

Run: `uvx --with tox-uv tox -l` Expected: lists exactly the six envs above (no
`py310`/`py311`/`django52`). Run: `PGPORT=5433 uvx --with tox-uv tox -e py312-django60`
Expected: floor env installs Django 6.0.x and the suite passes.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tox.ini .github/workflows/ci.yml CLAUDE.md tests/test_packaging.py
git commit -m "build: raise floor to Django 6.0 / Python 3.12 for django.tasks"
```

---

### Task 2: Config types + `AbsurdBackend` + `get_absurd_backends()`

**Files:**

- Create: `django_absurd/backends.py`
- Modify: `django_absurd/queues.py` (add `get_absurd_backends`)
- Modify: `tests/settings.py` (add a `TASKS` default alias — additive; old `ABSURD_*`
  path still works off defaults)
- Create: `tests/support_backends.py` (importable `ExtendedAbsurdBackend(AbsurdBackend)`
  for the subclass-resolution test)
- Test: `tests/test_backend.py` (new)

**Interfaces:**

- Produces:

  - `django_absurd/backends.py`: `class AbsurdBackend(BaseTaskBackend)` with instance
    attrs after `__init__`: `queues: set[str]`, `database: str`,
    `default_max_attempts: int`, `options: dict`. TypedDicts: reuse
    `absurd_sdk.CreateQueueOptions` for per-queue policy;
    `AbsurdBackendOptions(t.TypedDict, total=False)` with keys `DATABASE: str`,
    `DEFAULT_MAX_ATTEMPTS: int`, `QUEUES: dict[str, CreateQueueOptions]`.
    `enqueue`/`aenqueue`/`get_result`/`aget_result` raise `NotImplementedError`.
  - `queues.py`: `get_absurd_backends() -> dict[str, AbsurdBackend]` —
    `{alias: backend}` for `task_backends` aliases where
    `isinstance(backend, AbsurdBackend)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backend.py`:

```python
import pytest
from django_absurd.backends import AbsurdBackend
from django_absurd.queues import get_absurd_backends

ABSURD = "django_absurd.backends.AbsurdBackend"
EXTENDED = "tests.support_backends.ExtendedAbsurdBackend"


def test_default_alias_is_absurd_backend():
    from django.tasks import task_backends

    assert isinstance(task_backends["default"], AbsurdBackend)


def test_form_a_names_only(settings):
    from django.tasks import task_backends

    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["emails", "retained"]}}
    be = task_backends["default"]
    assert be.queues == {"emails", "retained"}
    assert be.database == "default"
    assert be.default_max_attempts == 5


def test_form_b_pushes_keys_up_and_reads_options(settings):
    from django.tasks import task_backends

    settings.TASKS = {"default": {
        "BACKEND": ABSURD,
        "OPTIONS": {
            "DATABASE": "absurd",
            "DEFAULT_MAX_ATTEMPTS": 9,
            "QUEUES": {"emails": {}, "retained": {"storage_mode": "partitioned"}},
        },
    }}
    be = task_backends["default"]
    assert be.queues == {"emails", "retained"}
    assert be.database == "absurd"
    assert be.default_max_attempts == 9


def test_get_absurd_backends_finds_default():
    backends = get_absurd_backends()
    assert set(backends) == {"default"}
    assert isinstance(backends["default"], AbsurdBackend)


def test_get_absurd_backends_matches_subclasses(settings):
    # ExtendedAbsurdBackend lives in an importable module so TASKS can name it by
    # path; the resolver must find it via isinstance, not class identity.
    from django.tasks import task_backends

    settings.TASKS = {"default": {"BACKEND": EXTENDED, "QUEUES": ["x"]}}
    backends = get_absurd_backends()
    assert set(backends) == {"default"}
    assert isinstance(backends["default"], AbsurdBackend)
    assert type(task_backends["default"]).__name__ == "ExtendedAbsurdBackend"


def test_enqueue_not_implemented_in_sp1():
    from django.tasks import task_backends

    with pytest.raises(NotImplementedError):
        task_backends["default"].enqueue(None, [], {})
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_backend.py -v` Expected: FAIL —
`ModuleNotFoundError: django_absurd.backends` / `TASKS` not configured.

- [ ] **Step 3: Implement minimal (prose — no production code block)**

- Create `django_absurd/backends.py`: import `typing as t`, `BaseTaskBackend` from
  `django.tasks.backends.base`, and `CreateQueueOptions` from `absurd_sdk`. Define
  `AbsurdBackendOptions` TypedDict (`total=False`) per the Interfaces block. Define
  `AbsurdBackend(BaseTaskBackend)`. In `__init__(self, alias, params)`: call
  `super().__init__(alias, params)` (sets `self.queues` from top-level `QUEUES`,
  `self.options` from `OPTIONS`); if `"QUEUES"` is present in `self.options`, reassign
  `self.queues = set(self.options["QUEUES"])` (Form B — push keys up); set
  `self.database = self.options.get("DATABASE", "default")` and
  `self.default_max_attempts = self.options.get("DEFAULT_MAX_ATTEMPTS", 5)`. Define
  `enqueue`, `aenqueue`, `get_result`, `aget_result` raising `NotImplementedError`. Do
  not raise on the both-forms mix (the `E002` check handles it in Task 4).
- In `queues.py`, add `get_absurd_backends()` returning the `isinstance`-filtered
  mapping over `task_backends` (import `task_backends` from `django.tasks` inside the
  function to avoid import-time settings access).
- In `tests/settings.py`, add a `TASKS` default alias whose `BACKEND` is `AbsurdBackend`
  and which declares a Form A `QUEUES` list plus `OPTIONS={"DATABASE": "default"}`. This
  is additive; the existing `ABSURD_*`-based code keeps working until Task 3.
- Create `tests/support_backends.py` with
  `class ExtendedAbsurdBackend(AbsurdBackend): pass` (importable by dotted path so the
  subclass test can name it in `TASKS`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_backend.py -v` Expected: PASS (6 tests). Run:
`uv run pytest` Expected: PASS (whole suite still green — change is additive).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py django_absurd/queues.py tests/settings.py tests/test_backend.py
git commit -m "feat: AbsurdBackend skeleton + typed config + get_absurd_backends"
```

---

### Task 3: Re-source `queues.py` readers; rewire callers; migrate ABSURD\_\* tests

**Files:**

- Modify: `django_absurd/queues.py` (reader signatures; delete `ABSURD_*` reads)
- Modify: `django_absurd/routers.py`, `django_absurd/checks.py`, `tests/conftest.py`
  (caller updates — behavior-preserving)
- Modify: `django_absurd/management/commands/absurd_sync_queues.py` (drop `--database`;
  sync all Absurd backends — the command keys on backends now, not DB aliases, so the
  flag migration belongs with the reader rewrite)
- Modify: `tests/test_queue_sync.py`, `tests/test_router_default.py`,
  `tests/test_checks.py` (drive via `TASKS`)

**Interfaces:**

- Consumes: `AbsurdBackend`, `get_absurd_backends()` (Task 2).
- Produces (in `queues.py`):
  - `get_declared_queues(backend: AbsurdBackend) -> dict[str, dict]` — Form B →
    `dict(backend.options["QUEUES"])`; Form A → `{name: {} for name in backend.queues}`.
  - `get_absurd_database(backend: AbsurdBackend) -> str` → `backend.database`.
  - `resolve_absurd_database() -> str` — the single distinct `.database` across
    `get_absurd_backends()`; `"default"` when none, and `"default"` when >1 distinct
    (degrade; `E004` flags it in Task 4).
  - `sync_queues(backend: AbsurdBackend) -> SyncResult` (unchanged reconcile logic,
    sourced from `get_declared_queues(backend)` on `backend.database`).
  - `get_absurd_client(using: str | None = None) -> Absurd` — `using` defaults to
    `resolve_absurd_database()`.
  - Unchanged: `SyncResult`, `MUTABLE_OPTION_KEYS`, `validate_backend(using)`,
    `BACKEND_ERR`.
- Produces (command): `absurd_sync_queues` with **no** `--database` argument; iterates
  all Absurd backends.

- [ ] **Step 1: Write/convert the failing tests**

Add to `tests/test_backend.py` (reader behavior):

```python
from django_absurd.queues import (
    get_declared_queues,
    resolve_absurd_database,
)


def test_get_declared_queues_form_a_defaults_policy(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["a", "b"]}}
    be = get_absurd_backends()["default"]
    assert get_declared_queues(be) == {"a": {}, "b": {}}


def test_get_declared_queues_form_b_preserves_policy(settings):
    settings.TASKS = {"default": {
        "BACKEND": ABSURD,
        "OPTIONS": {"QUEUES": {"a": {}, "b": {"cleanup_limit": 50}}},
    }}
    be = get_absurd_backends()["default"]
    assert get_declared_queues(be) == {"a": {}, "b": {"cleanup_limit": 50}}


def test_resolve_absurd_database_single(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "default"}}}
    assert resolve_absurd_database() == "default"


def test_resolve_absurd_database_ambiguous_degrades_to_default(settings):
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "default"}},
        "other": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "absurd"}},
    }
    assert resolve_absurd_database() == "default"
```

Convert `tests/test_queue_sync.py` and `tests/test_checks.py` and
`tests/test_router_default.py` to drive config via `settings.TASKS` instead of
`ABSURD_QUEUES`/`ABSURD_DATABASE`. Add a module-level helper in each and reassign
`settings.TASKS` wholesale. Example conversions (apply the same shape to every test that
set `ABSURD_*`):

```python
# tests/test_queue_sync.py — helper + representative conversions
ABSURD = "django_absurd.backends.AbsurdBackend"


def tasks_with(queues, database="default"):
    return {"default": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": database, "QUEUES": queues}}}


def test_sync_creates_with_options_and_model_maps(settings):
    settings.TASKS = tasks_with({"x": {"storage_mode": "partitioned", "cleanup_ttl": "90 days"}})
    call_command("absurd_sync_queues")
    q = Queue.objects.get(queue_name="x")
    assert q.storage_mode == "partitioned"
    assert q.cleanup_ttl == timedelta(days=90)
    assert table_exists("t_x")


def test_list_shorthand(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["alpha"]}}
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="alpha").exists()


def test_get_absurd_database_resolves_from_backend(settings):
    settings.TASKS = tasks_with({}, database="default")
    assert resolve_absurd_database() == "default"
    settings.TASKS = tasks_with({}, database="absurd")
    assert resolve_absurd_database() == "absurd"


def test_sync_command_takes_no_database_flag(settings):
    settings.TASKS = tasks_with({})
    with pytest.raises(TypeError):
        call_command("absurd_sync_queues", database="sqlite")


def test_sync_command_reports_nothing_when_no_absurd_backend(settings, capsys):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    call_command("absurd_sync_queues")
    assert "No Absurd task backends configured." in capsys.readouterr().out


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_sync_command_screams_on_non_postgres_backend(settings):
    settings.TASKS = tasks_with({"x": {}}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("absurd_sync_queues")


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_migrate_screams_on_non_postgres_backend(settings):
    settings.TASKS = tasks_with({}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("migrate", "django_absurd", database="sqlite", verbosity=0)
```

For `test_checks.py`, keep every assertion identical (same
`W001_MSG`/`W002_MSG`/`E001_MSG`/`W003_MSG` strings and `absurd.WNNN` ids) but build
state via `settings.TASKS = tasks_with(...)`; for the wrong-backend and router-missing
tests set `database="sqlite"` / `database="absurd"` inside `tasks_with`. For
`test_router_default.py`, no config change is needed (default backend on `"default"`),
but confirm it still reads `resolve_absurd_database()`.

Delete the two obsolete tests that asserted the removed surface:
`test_sync_command_uses_absurd_database_setting` and
`test_get_absurd_database_default_and_override` (replaced by the backend-resolved
equivalents above). The two `*_screams_*` tests above replace their
`ABSURD_DATABASE = "sqlite"` originals (drop the `database="sqlite"` kwarg from the sync
one; `migrate` keeps its own `--database` and sets the backend DB to `sqlite` via
`TASKS` so the router does not route it away).

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_backend.py -v` Expected: FAIL —
`get_declared_queues`/`resolve_absurd_database` take no/!backend arg yet, or read
`ABSURD_*`.

- [ ] **Step 3: Implement minimal (prose)**

- Rewrite `queues.py` readers to the Interfaces signatures.
  `get_declared_queues(backend)` normalizes either form. `get_absurd_database(backend)`
  returns `backend.database`. Add `resolve_absurd_database()` computing the
  distinct-`.database` set over `get_absurd_backends()` and degrading to `"default"` for
  0 or >1. `sync_queues(backend)` iterates `get_declared_queues(backend)` against
  `backend.database`. `get_absurd_client(using=None)` defaults `using` to
  `resolve_absurd_database()`. Delete all
  `getattr(settings, "ABSURD_QUEUES"/"ABSURD_DATABASE", ...)`.
- Update callers behavior-preserving: `routers.py` calls `resolve_absurd_database()`.
  `checks.py` resolves the default Absurd backend
  (`get_absurd_backends().get("default")`); if absent, returns `[]`; otherwise uses
  `get_declared_queues(that_backend)` and `get_absurd_database(that_backend)` — keep the
  SINGLE combined `check_absurd_queues` function for now (split is Task 4).
  `tests/conftest.py` `_reset_absurd_queues` keeps calling `get_absurd_client()` (now
  resolves via `resolve_absurd_database()`); keep its three caught exception types.
- Rewrite the `absurd_sync_queues` command to its final form: remove
  `add_arguments`/`--database`. In `handle`, call `get_absurd_backends()`; if empty,
  write `"No Absurd task backends configured."` and return; otherwise iterate the
  backends, `sync_queues(backend)` each, and write the
  created/reconciled/`storage_warnings` lines (prefix each line with the alias when more
  than one Absurd backend). Preserve the "No queues to sync." line for a backend with no
  declared queues. (The command keys on backends, not DB aliases — the flag removal is
  inseparable from the `sync_queues(backend)` signature change, so it lands here, not in
  a later task.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest` Expected: PASS (whole single-DB suite green; no `ABSURD_*`
remaining). Run: `grep -rn "ABSURD_QUEUES\|ABSURD_DATABASE" django_absurd tests`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/queues.py django_absurd/routers.py django_absurd/checks.py django_absurd/management tests/conftest.py tests/test_backend.py tests/test_queue_sync.py tests/test_checks.py tests/test_router_default.py
git commit -m "refactor: source queue config from AbsurdBackend; drop ABSURD_* settings"
```

---

### Task 4: Split checks (config always-on; DB-state `Tags.database`-gated) + E002/E003/E004

**Files:**

- Modify: `django_absurd/checks.py`
- Modify: `django_absurd/apps.py`
- Modify: `tests/test_checks.py`

**Interfaces:**

- Consumes: `get_absurd_backends()`, `get_declared_queues(backend)`,
  `get_absurd_database(backend)`, `validate_backend`, `BACKEND_ERR` (Task 3).
- Produces (`checks.py`):

  - `check_absurd_config(*, app_configs, **kwargs) -> list[CheckMessage]` — E001, E002,
    E003, E004, W003 (no DB-state queries).
  - `check_absurd_queue_state(*, app_configs, databases, **kwargs) -> list[CheckMessage]`
    — W001, W002; returns `[]` when `databases` is falsy or the backend DB is not in
    `databases`.
  - Message constants: keep `E001_MSG`, `W001_MSG`, `W002_MSG`, `W003_MSG`; add
    `E002_MSG`, `E003_MSG`, `E004_MSG` (and matching `*_HINT`).

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/test_checks.py` keeping the existing W001/W002/E001/W003 tests (converted
to `settings.TASKS` in Task 3) and adding:

```python
from django_absurd.checks import E002_MSG, E003_MSG, E004_MSG

ABSURD = "django_absurd.backends.AbsurdBackend"


def test_both_queue_forms_set_errors(settings, capsys):
    settings.TASKS = {"default": {
        "BACKEND": ABSURD, "QUEUES": ["a"], "OPTIONS": {"QUEUES": {"a": {}}},
    }}
    out = run_absurd_check(capsys)
    assert "absurd.E002" in out
    assert E002_MSG in out


def test_invalid_policy_key_errors(settings, capsys):
    settings.TASKS = {"default": {
        "BACKEND": ABSURD, "OPTIONS": {"QUEUES": {"a": {"bogus_key": 1}}},
    }}
    out = run_absurd_check(capsys)
    assert "absurd.E003" in out
    assert "a" in out


def test_invalid_storage_mode_literal_errors(settings, capsys):
    settings.TASKS = {"default": {
        "BACKEND": ABSURD, "OPTIONS": {"QUEUES": {"a": {"storage_mode": "nope"}}},
    }}
    out = run_absurd_check(capsys)
    assert "absurd.E003" in out


def test_multiple_backends_distinct_db_errors(settings, capsys):
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "default", "QUEUES": {"a": {}}}},
        "other": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "absurd", "QUEUES": {"b": {}}}},
    }
    out = run_absurd_check(capsys)
    assert "absurd.E004" in out


def test_plain_check_skips_db_state(settings, capsys):
    # Declared-but-unsynced queue would be W002 drift IF the DB check ran.
    settings.TASKS = tasks_with({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = tasks_with({"synced": {}, "missing": {}})
    out = run_absurd_check(capsys)  # plain `check`, no --database
    assert "absurd.W002" not in out


def test_check_with_database_runs_db_state(settings, capsys):
    settings.TASKS = tasks_with({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = tasks_with({"synced": {}, "missing": {}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" in out
```

Refactor the existing `run_absurd_check(capsys)` helper to forward args to the check
command and capture stdout/stderr exactly once (calling `capsys.readouterr()` twice
clears the buffer on the first read — the second returns empty):

```python
def run_absurd_check(capsys, *args, **kwargs):
    try:
        call_command("check", "django_absurd", *args, **kwargs)
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err
```

Note: the DB-state warnings are now gated, so the existing `test_drift_warns_run_sync` /
`test_option_drift_warns` / `test_duration_drift_warns` / `test_schema_absent_warns_*`
(which assert W001/W002) must call `run_absurd_check(capsys, databases=["default"])`.
The E001/E002/E003/E004/W003 tests use plain `run_absurd_check(capsys)` (config check
runs unconditionally).

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_checks.py -v` Expected: FAIL —
`E002_MSG`/`E003_MSG`/`E004_MSG` import errors; gating not implemented.

- [ ] **Step 3: Implement minimal (prose)**

- In `checks.py`, split the single check into `check_absurd_config` and
  `check_absurd_queue_state`. Add the new message constants (`E002`: both queue forms
  set; `E003`: invalid per-queue policy; `E004`: multiple Absurd backends with distinct
  `DATABASE`). Each `msg` states only the problem; each `hint` the resolution.
- `check_absurd_config` iterates `get_absurd_backends()`: detect both top-level `QUEUES`
  and `OPTIONS["QUEUES"]` present → E002; for each queue policy dict validate keys ∈
  `set(CreateQueueOptions.__annotations__)` and that `storage_mode`/`detach_mode` values
  ∈ their SDK literal sets — derive valid keys/literals by introspecting
  `CreateQueueOptions` (import it + `QueueStorageMode`/`QueueDetachMode` from
  `absurd_sdk`, use `t.get_args`); name the offending queue in the message → E003. Run
  `validate_backend(backend.database)` catching `ImproperlyConfigured`→E001 and
  `OperationalError`→skip. W003 when `backend.database != "default"` and the router is
  absent. Across all backends, if the set of distinct `.database` values has length > 1
  → E004.
- `check_absurd_queue_state(*, app_configs, databases, **kwargs)`:
  `if not databases: return []`. For each Absurd backend whose
  `backend.database in databases`, run the existing schema-absent (W001) and drift
  (W002) queries against `connections[backend.database]` (reuse the
  `query_queue_state`/`has_option_drifted`/`parse_interval` helpers, now taking the
  backend's alias).
- `apps.py` `ready()`: `from django.core.checks import Tags, register`;
  `register(checks.check_absurd_config, "absurd")`;
  `register(checks.check_absurd_queue_state, Tags.database, "absurd")`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_checks.py -v` Expected: PASS. Run: `uv run pytest`
Expected: whole single-DB suite green.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/checks.py django_absurd/apps.py tests/test_checks.py
git commit -m "feat: split checks (config + Tags.database-gated state); add E002/E003/E004"
```

---

### Task 5: Migrate the multi-DB suite to `TASKS`

**Files:**

- Modify: `tests/multidb/settings.py` (drop `ABSURD_DATABASE`; add `TASKS` with
  `OPTIONS["DATABASE"]="absurd"`)
- Modify: `tests/multidb/test_check.py`, `tests/multidb/test_router.py` (drive via
  `TASKS`; pass `databases` for state checks)
- Modify: `CLAUDE.md` testing note if it still references `ABSURD_*`

**Interfaces:**

- Consumes: everything from Tasks 2–4.
- Produces: a green `tests/multidb` suite proving routing/provisioning/command/W002 on
  the non-default alias.

- [ ] **Step 1: Convert the multidb settings + tests**

Edit `tests/multidb/settings.py`: remove `ABSURD_DATABASE = "absurd"`; add (after the
`from tests.settings import *`):

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {"DATABASE": "absurd"},
    },
}
```

Keep `DATABASE_ROUTERS` and the two-alias `DATABASES` block exactly as-is. In
`tests/multidb/test_check.py`, drive declared queues via `settings.TASKS` (helper that
nests `OPTIONS={"DATABASE":"absurd","QUEUES":{...}}`) and call the state check with
`databases=["absurd"]`:

```python
ABSURD = "django_absurd.backends.AbsurdBackend"


def tasks_absurd(queues):
    return {"default": {"BACKEND": ABSURD, "OPTIONS": {"DATABASE": "absurd", "QUEUES": queues}}}


def test_duration_drift_detected_on_non_default_alias(settings, capsys):
    settings.TASKS = tasks_absurd({"d": {"cleanup_ttl": "90 days"}})
    call_command("absurd_sync_queues")
    settings.TASKS = tasks_absurd({"d": {"cleanup_ttl": "30 days"}})
    out = run_absurd_check(capsys, databases=["absurd"])  # same single-read helper
    assert "absurd.W002" in out
```

(The multidb suite's `run_absurd_check` helper is refactored to the same
`*args, **kwargs` single-read form as the main suite's.)

In `tests/multidb/test_router.py`, the routing/`allow_migrate`/`db_for_*` tests need no
config change (the session-global `TASKS` DATABASE is `absurd`). Convert
`test_sync_command_honors_alias` to set `settings.TASKS = tasks_absurd({"routed": {}})`
then `call_command("absurd_sync_queues")` and assert
`Queue.objects.get(queue_name="routed")`.

- [ ] **Step 2: Run to verify failures (pre-impl already done; this confirms wiring)**

Run: `PGPORT=5433 uv run pytest tests/multidb -v` Expected: initially FAIL where tests
still reference `ABSURD_*`; after the edits above, the failures are only assertion
wiring (no new production code needed — Tasks 2–4 already implemented behavior).

- [ ] **Step 3: Implement minimal (prose)**

No new production code. If a multidb test reveals a gap (e.g.
`resolve_absurd_database()` not returning `"absurd"` because the alias maps through
`OPTIONS["DATABASE"]`), fix in `queues.py` — but the Task 3 implementation already
covers it. Confirm `CLAUDE.md` has no stale `ABSURD_*` references; update the testing
note if present.

- [ ] **Step 4: Run to verify pass**

Run: `PGPORT=5433 uv run pytest tests/multidb -v` Expected: PASS. Run:
`PGPORT=5433 uvx --with tox-uv tox` Expected: full matrix green (all six envs).

- [ ] **Step 5: Commit**

```bash
git add tests/multidb CLAUDE.md
git commit -m "test: migrate multi-DB suite to TASKS-sourced config"
```

---

## Self-Review

**Spec coverage:** version bump (T1); config schema + `AbsurdBackend` + typed config +
`isinstance` resolution (T2); reader re-sourcing + router + `resolve_absurd_database` +
deletions + no-flag command syncing all backends (T3); check split + `Tags.database`
gating + E002/E003/E004 + apps registration (T4); multidb migration + W002-on-alias
(T5). All spec sections map to a task.

**Type consistency:** `get_declared_queues(backend)`, `get_absurd_database(backend)`,
`resolve_absurd_database()`, `sync_queues(backend)`,
`get_absurd_backends() -> dict[str, AbsurdBackend]`, and
`AbsurdBackend.{queues,database,default_max_attempts,options}` are used identically
across T2–T5.

**Placeholder scan:** no TBD/TODO; every code step shows full test code; implementation
steps are prose per the project's no-coding-ahead rule.

**Note on E004 placement:** E004 is pure config inspection (no DB I/O), so it is fully
unit-tested in the single-DB suite (T4, two `settings`-fixture backends with distinct
`DATABASE`s) — no real second DB needed.

**Convention check (CLAUDE.md):** `import typing as t` only (backends.py `t.TypedDict`);
absolute imports; verb-named module functions; helpers placed below their public
callers; pytest function-based with autouse `_enable_db`,
`transaction=True`/`databases=[...]` markers only where commits/DDL/multi-DB are needed;
no mocks — checks & commands driven by `call_command` + `capsys` asserting full message
text; check `msg`=problem / `hint`=resolution; psycopg3 asserted via `validate_backend`.
Config driven through the `settings` fixture (wholesale `settings.TASKS=` reassignment
fires `setting_changed`, resetting `task_backends`).
