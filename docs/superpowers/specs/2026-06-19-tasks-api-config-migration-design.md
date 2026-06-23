# django-absurd — Spec: Migrate queue config to the Django Tasks API (SP1)

Date: 2026-06-19 Status: approved-for-planning

First sub-project of binding django-absurd to Django's Tasks framework (`django.tasks`,
new in Django 6.0). SP1 moves queue configuration off the bespoke `ABSURD_QUEUES` /
`ABSURD_DATABASE` settings (specs 2–3) and onto a `TASKS` backend alias. Defines an
`AbsurdBackend(BaseTaskBackend)` that carries config; re-sources the sync command,
system checks, and DB router from it; deletes the old settings.

**No task execution in SP1.** `enqueue`/`get_result` are SP2; the worker command is SP3.
Backend exists only to hold config + the queue allowlist.

## Why

`django.tasks` is the canonical Django task abstraction. Binding to it means config
lives where Django users expect (`TASKS`), the backend's queue allowlist comes for free
(`self.queues`), and SP2/SP3 plug in as `enqueue`→`spawn` + a consuming worker. The
parallel `ABSURD_QUEUES`/`ABSURD_DATABASE` settings and the standalone
`@app.register_task` registry idea are dropped — Django's `@task` + `Task.module_path`
become the registry in SP2/SP3.

## Global constraints (version bump — prerequisite)

`django.tasks` and `BaseTaskBackend` ship only in Django 6.0; Django 6.0 requires Python
3.12+. So SP1 raises the floor:

- `pyproject.toml`: `requires-python = ">=3.12"`; `[project] dependencies` `Django>=6.0`
  (was `>=5.2`). ruff `target-version = "py312"`.
- `tox.ini`: drop the `django52` factor + py310/py311. `env_list`:
  `py3{12,13,14}-django60`, `latest`, `py312-django60-mypy`, `py314-django60-mypy`.
  Floor env `py312-django60` resolves `lowest-direct`; the rest `highest`. Drop the
  `django52` lines from `deps` / `uv_resolution`.
- `.github/workflows/ci.yml`: matrix →
  `[py312-django60, py313-django60, py314-django60, latest, py312-django60-mypy, py314-django60-mypy]`.
- `CLAUDE.md` Runtime note: floor is Django 6.0 / Python 3.12.

Single Django minor (6.0) collapses "min-max Django" to a Python spread; min-max mypy
stays (py3.12 floor vs py3.14 ceiling, both Django 6.0).

## Config schema (`TASKS`)

A `TASKS` alias whose `BACKEND` resolves to an `AbsurdBackend` (or subclass). Queue
declaration takes ONE of two mutually-exclusive forms. Minimum to make a queue work is a
name; policy is optional.

**Form A — names only (default policy), Django-native top-level `QUEUES`:**

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["emails", "retained"],
        "OPTIONS": {"DATABASE": "default", "DEFAULT_MAX_ATTEMPTS": 5},
    },
}
```

**Form B — per-queue policy, under `OPTIONS["QUEUES"]` (name → `CreateQueueOptions`):**

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "DATABASE": "default",
            "DEFAULT_MAX_ATTEMPTS": 5,
            "QUEUES": {
                "emails": {},
                "retained": {
                    "storage_mode": "partitioned",
                    "cleanup_ttl": "90 days",
                    "cleanup_limit": 2000,
                    "partition_lookahead": "42 days",
                    "detach_mode": "empty",
                },
            },
        },
    },
}
```

Rules:

- `DATABASE` (a `DATABASES` alias, default `"default"`) and `DEFAULT_MAX_ATTEMPTS`
  (default `5`) are **backend-wide** — always in `OPTIONS`, both forms.
- Per-queue policy keys = the absurd-sdk `CreateQueueOptions` set: `storage_mode`
  (`"unpartitioned"|"partitioned"`, create-only/immutable), and the mutable policy keys
  `partition_lookahead`, `partition_lookback`, `cleanup_ttl` (durations as `str`),
  `cleanup_limit` (`int`), `detach_mode` (`"none"|"empty"`), `detach_min_age` (`str`).
  Matches existing `MUTABLE_OPTION_KEYS` + storage-mode-immutable handling in
  `queues.py`.
- **Either/or, never both.** Setting both top-level `QUEUES` and `OPTIONS["QUEUES"]` is
  a config error (system check `E002`).
- No queue config at all → defaults to a single `"default"` queue, default policy
  (matches Django's `DEFAULT_TASK_QUEUE_NAME` and today's `{"default": {}}`).

## `AbsurdBackend` (`django_absurd/backends.py`)

`class AbsurdBackend(BaseTaskBackend)`. `__init__(self, alias, params)`:

- `super().__init__(alias, params)` — sets `self.queues` from top-level `QUEUES`
  (default `{"default"}`) and `self.options` from `OPTIONS`.
- If `OPTIONS["QUEUES"]` present (Form B): **push its keys up** —
  `self.queues = set(self.options["QUEUES"])` — so Django's `validate_task` allowlist
  works regardless of form.
- Stash `self.database = self.options.get("DATABASE", "default")` and
  `self.default_max_attempts = self.options.get("DEFAULT_MAX_ATTEMPTS", 5)`.
- **Non-throwing** on the both-forms mix (deterministic: Form B wins for `self.queues`);
  the `E002` system check is what surfaces/blocks it, so constructing the backend to
  inspect it never raises.

`enqueue` / `aenqueue` / `get_result` / `aget_result` → `raise NotImplementedError`
(SP2). Support flags left at `BaseTaskBackend` defaults in SP1 (declared in SP2 when the
methods land — nothing calls them while enqueue is unimplemented).

## Resolving Absurd backends (`django_absurd/queues.py`)

```python
def get_absurd_backends() -> dict[str, AbsurdBackend]:
    from django.tasks import task_backends
    return {
        alias: be
        for alias in task_backends
        if isinstance((be := task_backends[alias]), AbsurdBackend)
    }
```

**`isinstance`, not class identity** — developers may subclass `AbsurdBackend`.

Re-sourced readers (replace the `ABSURD_*` settings):

- `get_declared_queues(backend) -> dict[str, dict]` — normalizes either form to
  `{name: policy}` (`{}` = default): Form B → `dict(options["QUEUES"])`; Form A →
  `{name: {} for name in backend.queues}`.
- `get_absurd_database(backend) -> str` → `backend.database`.
- `resolve_absurd_database() -> str` (router/default helper) — the single distinct
  `DATABASE` across `get_absurd_backends()`; `"default"` if none; if >1 distinct,
  `"default"` (degrade) while `E004` flags the ambiguity.
- `sync_queues(backend) -> SyncResult` — unchanged reconcile logic, sourced from
  `get_declared_queues(backend)` on `backend.database`. Keep `SyncResult`,
  `MUTABLE_OPTION_KEYS`, `validate_backend`, `BACKEND_ERR`, `get_absurd_client`.
- `get_absurd_client(using=None)` — `using` defaults to `resolve_absurd_database()`.

## Command: `absurd_sync_queues` (no flag)

**Drop `--database`.** No flag. Iterate `get_absurd_backends()`, `sync_queues(be)` each,
report per backend (alias-prefixed when >1). No Absurd backends → "No Absurd task
backends configured." Empty queues for a backend → "No queues to sync."

## System checks (`django_absurd/checks.py` + `apps.py`)

Split into two registered functions so DB work is gated by the `databases` arg.

**Config check — `check_absurd_config`** (register with tag `"absurd"`, runs always, no
DB state I/O). For each backend in `get_absurd_backends()`:

- `E002` — both top-level `QUEUES` and `OPTIONS["QUEUES"]` set. msg: problem; hint: pick
  one.
- `E003` — invalid queue policy: an `OPTIONS["QUEUES"]` value has an unknown key, or
  `storage_mode`/`detach_mode` outside its literal set. msg names the queue + bad key.
- `E001` — wrong backend: `validate_backend(backend.database)` raises
  `ImproperlyConfigured` (DB engine not psycopg3). `OperationalError` (can't connect) →
  skip (transient). Connecting to assert the engine class is a config check, not queue
  state — stays here.
- `W003` — `backend.database != "default"` and `AbsurdRouter` not in `DATABASE_ROUTERS`
  (tolerant of import-path string OR instance).
- `E004` — >1 distinct `DATABASE` across Absurd backends (router can't route the
  `django_absurd` app to multiple DBs). Reuses spec-3's single-routed-DB assumption;
  multi-DB routing stays deferred.

**DB-state check — `check_absurd_queue_state`** (register with `Tags.database` + tag
`"absurd"`; guard `if not databases: return []`). For each backend whose
`backend.database in databases`:

- `W001` — absurd schema absent (`ProgrammingError` querying `Queue`).
- `W002` — declared queues drift from DB (existing existence + interval-drift logic,
  `parse_interval` on `connections[backend.database]`).

A plain `manage.py check` / `runserver` passes no `databases` → W001/W002 skipped (no DB
hammering). `migrate`, `check --database <alias>`, and the SP3 worker (runs `check` with
the backend's DB at startup) include it. Fully implements the deferred `Tags.database`
split.

`apps.py` `ready()`:

```python
from django.core.checks import Tags, register
register(checks.check_absurd_config, "absurd")
register(checks.check_absurd_queue_state, Tags.database, "absurd")
```

## Router (`django_absurd/routers.py`)

`AbsurdRouter` unchanged in shape; `get_absurd_database()` call sites →
`resolve_absurd_database()`. Routes ONLY the `django_absurd` app (non-prescriptive, as
spec 3). Single Absurd DB → routes there; none → no-op on `default`; ambiguous →
degrades to `default` with `E004` raised.

## Deletions + settings migration

- Remove `ABSURD_QUEUES` + `ABSURD_DATABASE` reads everywhere
  (`getattr(settings, ...)`).
- `tests/settings.py`: replace any `ABSURD_*` with a `TASKS` default alias
  (`AbsurdBackend`, Form A `QUEUES`, `OPTIONS["DATABASE"]="default"`). Keep
  `DATABASE_ROUTERS=[AbsurdRouter]` (no-op at default).
- `tests/multidb/settings.py`: `TASKS["default"]["OPTIONS"]["DATABASE"]="absurd"`
  (session-global), router registered, two Postgres aliases with `_multidb` `TEST.NAME`s
  (unchanged pattern).
- `tests/conftest.py` `_reset_absurd_queues`: `get_absurd_client()` (now resolves via
  `resolve_absurd_database()`); still catches `OperationalError`/`ProgrammingError`/
  `ImproperlyConfigured`.

## Testing (pytest, function-based, real Postgres via compose; two suites)

Drive checks/command by running them and asserting full emitted message text (per
project conventions). Keep the nested `tests/multidb/` suite pattern.

Main suite (`tests/`):

- **Backend config:** `task_backends["default"]` is an `AbsurdBackend`;
  `get_absurd_backends()` finds it; a subclass registered in `TASKS` is still found
  (isinstance, not identity).
- **Either-form parsing:** Form A → `self.queues == {"emails","retained"}`,
  `get_declared_queues` → `{name: {}}`. Form B → `self.queues` = keys, policy preserved.
- **`E002`:** both `QUEUES` + `OPTIONS["QUEUES"]` set → `call_command("check")` emits
  `absurd.E002`.
- **`E003`:** bogus policy key / bad `storage_mode` literal → `absurd.E003` naming the
  queue.
- **`E001`:** Form A backend on a sqlite `DATABASE` → `absurd.E001` (full
  `BACKEND_ERR`); reset fixture stays green.
- **`W003`:** `DATABASE != "default"` + `DATABASE_ROUTERS=[]` → `absurd.W003`
  (settings-only).
- **`databases`-gating:** plain `call_command("check","django_absurd")` emits NO
  `W001`/`W002` even with a drifted/absent schema; `check --database default` (or
  `databases=["default"]`) DOES.
- **Command (no flag):** `absurd_sync_queues` creates declared queues on the backend's
  DB and the ORM reads them back; reports per-backend.
- **Router default no-op:** `Queue.objects.db == "default"`; specs 1–2 tests green.

Multi-DB suite (`tests/multidb/`):

- **Routing + provisioning on alias:** `Queue.objects.db == "absurd"`; schema present on
  `absurd`, absent on `default`; `allow_migrate` contract (`True`
  absurd/`django_absurd`, `False` default/`django_absurd`, `None` absurd/`auth`).
- **Command honors backend DB:** `absurd_sync_queues` (no flag) creates on `absurd`.
- **`W002` on non-default alias** via `check --database absurd`: interval parsing runs
  on `connections["absurd"]`.
- **`E004`:** two `TASKS` Absurd backends with distinct `DATABASE`s → `absurd.E004`.

Router ripples in the main suite stay fixed with targeted per-test markers
(migrate-guard test sets its backend DB to `sqlite`;
`test_no_pending_migrations_for_app` gets `databases=["default","sqlite"]`) — never a
global collection hook.

## Out of scope (later sub-projects)

- **SP2 — produce:** `AbsurdBackend.enqueue`/`aenqueue` →
  `client.spawn(task.module_path, …)`; `get_result`/`aget_result` ← `fetch_task_result`
  mapping; support flags. Point-of- use validation: enqueue to a missing
  queue/unmigrated schema fails clearly.
- **SP3 — consume:** `absurd_worker` command (sync + async) consuming via `module_path`
  dispatch; runs the DB-state check at startup; task→queue typo cross-check (needs
  `tasks.py` discovery).
- Multi-DB routing across multiple Absurd backends; `run_after`/defer; priority.
