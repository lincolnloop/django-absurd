# Command error translation — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every django-absurd management command turns a configuration failure into a
clean `CommandError` instead of a traceback.

**Architecture:** One `AbsurdCommand` base overrides `execute`, catching
`ImproperlyConfigured` and `DjangoAbsurdError` and re-raising `CommandError` chained
`from exc`; all six commands inherit it. One typed `SchemaNotInstalledError` replaces
the three hand-rolled `ImproperlyConfigured` schema messages and covers two paths that
leak raw psycopg errors today.

**Tech Stack:** Django 6.0 management commands, absurd-sdk, psycopg 3, pytest +
pytest-django.

Spec:
[`../specs/2026-08-15-command-error-translation-design.md`](../specs/2026-08-15-command-error-translation-design.md).
Issue: [#128](https://github.com/lincolnloop/django-absurd/issues/128).

## Global Constraints

- Django 6.0+ / Python 3.12+; psycopg (v3) backend only.
- `import typing as t` — never `from typing import X`. Absolute imports only.
- Functions carry a verb. No leading-underscore module constants or helpers. Helpers go
  BELOW the public function that uses them.
- Re-raising inside an `except` always chains `from exc` — never `from None`. Classify
  first; re-raise the original untouched when the error is not what the curated message
  claims.
- Exceptions own their messages; callers never assemble text.
- Tests: pytest, function-based, integration-level through real entrypoints. Assert the
  COMPLETE error message, never a fragment. No mocks/monkeypatching. Autouse
  `_enable_db` gives DB access — add `@pytest.mark.django_db(transaction=True)` only for
  commits/DDL. Alphabetize fixture params and parametrize values.
- 100% statement + branch coverage on lines this plan adds or changes.
- **Test runs are targeted, never the full matrix.** While iterating, run the single
  test under change: `uv run pytest <path>::<test> -q --no-cov`. When several fail at
  once, close the gap with `uv run pytest <path> -q --no-cov --lf` rather than widening
  to a suite. Before a commit, the touched files only. `uvx --with tox-uv tox -e dev` is
  the coordinator's, run once at the end — never between commits, and never the bare
  `tox` matrix. Never invoke ruff/mypy directly; `uv run pre-commit run --files <paths>`
  covers them.
- `docker compose up -d db db_pg_cron` must be running.
- Commit titles are the changelog. The type-switch commit is `feat!`.

## File Structure

| File                                                                 | Responsibility                                                                   |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `django_absurd/exceptions.py`                                        | gains `SchemaNotInstalledError`                                                  |
| `django_absurd/management/base.py`                                   | gains `AbsurdCommand`; `AbsurdReportCommand` re-parents onto it                  |
| `django_absurd/management/commands/*.py` (5)                         | inherit the base, lose their hand-rolled translations                            |
| `django_absurd/pg_cron/management/commands/absurd_sync_crons.py`     | same                                                                             |
| `django_absurd/queues.py`                                            | schema probe raises the typed error; gains a classified queue-listing entrypoint |
| `django_absurd/backends.py`, `django_absurd/worker.py`               | schema probes raise the typed error                                              |
| `django_absurd/cleanup.py`                                           | new classified schema probe                                                      |
| `tests/core/test_command_errors.py`                                  | NEW — one case per core command on a hidden schema                               |
| `tests/pg_cron/test_absurd_sync_crons_command.py`                    | gains the pg_cron command's case                                                 |
| `tests/core/test_queue_sync.py`, `test_worker.py`, `test_enqueue.py` | three existing assertions change type                                            |

---

### Task 1: `SchemaNotInstalledError` replaces the three hand-rolled messages

No behavior change beyond the exception type — the three sites already raise on the same
condition. This task exists on its own so the type switch is reviewable apart from the
base class.

**Files:**

- Modify: `django_absurd/exceptions.py`
- Modify: `django_absurd/queues.py:113`, `django_absurd/backends.py:200`,
  `django_absurd/worker.py:297`
- Test: `tests/core/test_queue_sync.py:45`, `tests/core/test_worker.py:92`,
  `tests/core/test_enqueue.py:160`

**Interfaces:**

- Produces: `django_absurd.exceptions.SchemaNotInstalledError`, a `DjangoAbsurdError`
  subclass taking NO constructor arguments, whose message is exactly
  `Absurd schema is not installed. Run: manage.py migrate`. Tasks 2 and 3 raise and
  assert it.

- [ ] **Step 1: Change the three existing assertions to the new type (RED)**

`tests/core/test_queue_sync.py` — only the first of the two nearby cases changes;
`test_migrate_screams_on_non_postgres_backend` is the psycopg3 check, a different
condition, and keeps `ImproperlyConfigured`:

```python
@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_sync_command_screams_on_non_postgres_backend(
    settings: Settings,
) -> None:
    settings.TASKS = build_tasks_setting({"x": {}}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("absurd_sync_queues")
```

Leave that one alone. Add a schema-absent case beside it asserting the new type and the
complete message:

```python
def test_sync_command_names_the_missing_schema(settings: Settings) -> None:
    settings.TASKS = build_tasks_setting({"x": {}})
    with (
        utils.hide_absurd_schema(),
        pytest.raises(
            SchemaNotInstalledError,
            match=r"^Absurd schema is not installed\. Run: manage\.py migrate$",
        ),
    ):
        call_command("absurd_sync_queues")
```

`tests/core/test_worker.py:92` — the async client probe. Swap the expected type and
assert the whole message rather than `match="migrate"`:

```python
    with (
        utils.hide_absurd_schema(),
        pytest.raises(
            SchemaNotInstalledError,
            match=r"^Absurd schema is not installed\. Run: manage\.py migrate$",
        ),
    ):
        asyncio.run(_enter())
```

`tests/core/test_enqueue.py:160` — the enqueue path. Same swap, same full-message match.

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/core/test_queue_sync.py::test_sync_command_names_the_missing_schema -q --no-cov
```

Expected: `ImportError` on `SchemaNotInstalledError` — the name does not exist yet. Same
for the other two amended tests; run each by node id rather than the whole file.

- [ ] **Step 3: Add the exception**

In `django_absurd/exceptions.py`, beside the other typed errors: a `DjangoAbsurdError`
subclass named `SchemaNotInstalledError`, no `__init__` parameters, passing the fixed
message up to `super().__init__`. Give it a docstring saying what raises it (queue
reconcile, the enqueue path, the worker's client probe, cleanup, flush) and that
`migrate`'s `post_migrate` provisions declared queues — which is why the message names
`migrate` alone and not `absurd_sync_queues` after it.

- [ ] **Step 4: Raise it at the three sites**

Each site keeps its existing `except` clause and its `from exc` chaining; only the
raised type changes and the local `msg` assignment goes away, because the exception owns
its text now.

- `queues.py:113` — the `ProgrammingError` handler around the catalog read.
- `backends.py:200` — the psycopg handler inside `enqueue`, the branch reached when the
  queue IS declared.
- `worker.py:297` — the async `list_queues` probe. Its longer wording
  (`then manage.py absurd_sync_queues`) disappears with the switch; that is intended.

- [ ] **Step 5: Run the three files again**

```bash
uv run pytest tests/core/test_queue_sync.py tests/core/test_worker.py tests/core/test_enqueue.py -q --no-cov --lf
```

Expected: PASS. `--lf` re-runs only what failed in the previous step; drop it for the
final pre-commit pass over the three touched files.

- [ ] **Step 6: Sweep for stale references**

```bash
grep -rn "schema is not installed" django_absurd tests docs README.md
```

Expected: only the exception class. Use `grep -rn`, not `ag` — ag under-reports in this
repo (it silently skips `django_absurd/pg_cron/` and `tests/`).

- [ ] **Step 7: Commit**

```bash
git add django_absurd/exceptions.py django_absurd/queues.py django_absurd/backends.py \
        django_absurd/worker.py tests/core/test_queue_sync.py \
        tests/core/test_worker.py tests/core/test_enqueue.py
git commit -m 'feat!: raise SchemaNotInstalledError, not ImproperlyConfigured, when Absurd is unmigrated'
```

---

### Task 2: `AbsurdCommand` base, six commands, four handlers deleted

**Files:**

- Modify: `django_absurd/management/base.py`
- Modify: `django_absurd/management/commands/absurd_beat.py`, `absurd_cleanup.py`,
  `absurd_flush.py`, `absurd_sync_queues.py`, `absurd_worker.py`
- Modify: `django_absurd/pg_cron/management/commands/absurd_sync_crons.py`
- Create: `tests/core/test_command_errors.py`
- Modify: `tests/pg_cron/test_absurd_sync_crons_command.py`

**Interfaces:**

- Consumes: `SchemaNotInstalledError` from Task 1.
- Produces: `django_absurd.management.base.AbsurdCommand`, a `BaseCommand` subclass
  overriding `execute(self, *args: t.Any, **options: t.Any) -> t.Any`.
  `AbsurdReportCommand` becomes `AbsurdCommand`'s subclass and keeps
  `report_sync_result` unchanged. Task 3 relies on both names.

- [ ] **Step 1: Write the failing tests (RED)**

New file `tests/core/test_command_errors.py`. Five core commands, one case each, driven
through `call_command` with the schema hidden. `absurd_beat` gets the no-backend
condition instead — it touches no database at start, so a hidden schema proves nothing
about it:

```python
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import Settings

from tests import utils

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA_ABSENT = "Absurd schema is not installed. Run: manage.py migrate"


@pytest.mark.parametrize("command", ["absurd_sync_queues", "absurd_worker"])
def test_a_command_names_the_missing_schema_without_a_traceback(
    command: str,
    settings: Settings,
) -> None:
    settings.TASKS = utils.make_tasks_settings(queues={"default": {}})
    with utils.hide_absurd_schema(), pytest.raises(CommandError) as excinfo:
        call_command(command)
    assert str(excinfo.value) == SCHEMA_ABSENT


def test_beat_reports_a_missing_backend_without_a_traceback(
    settings: Settings,
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}
    }
    with pytest.raises(CommandError) as excinfo:
        call_command("absurd_beat")
    assert str(excinfo.value) == (
        "No Absurd backend configured. Add a "
        "django_absurd.backends.AbsurdBackend entry to TASKS."
    )
```

`absurd_cleanup` and `absurd_flush` are deliberately absent from the parametrize list —
their probes land in Task 3, which adds their cases to this same file. Never commit a
test this task cannot turn green.

In `tests/pg_cron/test_absurd_sync_crons_command.py`, add the same no-backend case for
`absurd_sync_crons`, asserting the identical complete message.

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/core/test_command_errors.py -q --no-cov
```

Expected: both schema cases FAIL with `SchemaNotInstalledError` escaping uncaught. The
beat case passes already; it is a regression guard for the handler this task deletes.

- [ ] **Step 3: Add the base**

In `django_absurd/management/base.py`, above `AbsurdReportCommand`: `AbsurdCommand`,
subclassing `BaseCommand`, overriding `execute` to call
`super().execute(*args, **options)` inside a `try`, catching `ImproperlyConfigured` and
`DjangoAbsurdError`, re-raising `CommandError(str(exc)) from exc`. Return `super()`'s
value on the happy path — `absurd_sync_crons.handle` returns `str | None` and Django
writes a non-`None` return to stdout.

Comment the reason for `execute` over `handle`: it covers the system-check phase, needs
no command to rename its `handle`, and `--traceback` still prints the original chain.

Then re-parent `AbsurdReportCommand` onto `AbsurdCommand`. Its body does not change.

- [ ] **Step 4: Move all six commands onto it and delete the handlers**

- `absurd_beat`, `absurd_cleanup`, `absurd_flush`, `absurd_sync_crons`: `BaseCommand` →
  `AbsurdCommand`.
- `absurd_sync_queues`, `absurd_worker`: already `AbsurdReportCommand`, now transitively
  covered — no class change.
- Delete the four hand-rolled translations: the `BackendNotConfiguredError` handlers in
  `absurd_beat`, `absurd_worker` and `absurd_sync_crons`, and the `ImproperlyConfigured`
  handler around `provision_backend` in `absurd_worker`. Each becomes a bare call.
- Drop the imports those handlers left behind (`BackendNotConfiguredError`,
  `ImproperlyConfigured`, and `CommandError` where nothing else raises it —
  `absurd_beat`, `absurd_worker` and `absurd_sync_crons` all still raise `CommandError`
  directly elsewhere, so check each before removing).

- [ ] **Step 5: Run the command tests**

```bash
uv run pytest tests/core/test_command_errors.py tests/pg_cron/test_absurd_sync_crons_command.py -q --no-cov
```

Expected: every case PASSES.

- [ ] **Step 6: Run the command-driving tests**

```bash
uv run pytest tests/core/test_worker.py tests/core/test_command_output.py \
              tests/pg_cron/test_absurd_sync_crons_command.py -q --no-cov
```

Expected: PASS. `tests/core/test_worker.py:424` already asserts `CommandError` on the
schema condition and must stay green — the base now produces what the deleted handler
did.

- [ ] **Step 7: Commit**

```bash
git add django_absurd/management django_absurd/pg_cron/management \
        tests/core/test_command_errors.py tests/pg_cron/test_absurd_sync_crons_command.py
git commit -m 'feat: translate configuration failures to CommandError in one command base'
```

---

### Task 3: cleanup and flush stop leaking raw psycopg errors

**Files:**

- Modify: `django_absurd/cleanup.py`
- Modify: `django_absurd/queues.py`
- Modify: `django_absurd/management/commands/absurd_flush.py`
- Test: `tests/core/test_command_errors.py` (extend Task 2's parametrize list)

**Interfaces:**

- Consumes: `SchemaNotInstalledError` (Task 1), `AbsurdCommand` (Task 2).
- Produces:
  `django_absurd.queues.list_provisioned_queues(using: str | None = None) -> list[str]`,
  used by `absurd_flush`.

- [ ] **Step 1: Add the two cases (RED)**

In `tests/core/test_command_errors.py`, extend the existing parametrize list to
`["absurd_cleanup", "absurd_flush", "absurd_sync_queues", "absurd_worker"]`
(alphabetical, per the conventions) and pass `absurd_flush` its non-interactive flag,
since the command prompts before dropping anything:

```python
@pytest.mark.parametrize(
    "command",
    ["absurd_cleanup", "absurd_flush", "absurd_sync_queues", "absurd_worker"],
)
def test_a_command_names_the_missing_schema_without_a_traceback(
    command: str,
    settings: Settings,
) -> None:
    settings.TASKS = utils.make_tasks_settings(queues={"default": {}})
    with utils.hide_absurd_schema(), pytest.raises(CommandError) as excinfo:
        call_command(command, *(["--noinput"] if command == "absurd_flush" else []))
    assert str(excinfo.value) == SCHEMA_ABSENT
```

```bash
uv run pytest tests/core/test_command_errors.py -q --no-cov -k "cleanup or flush"
```

Expected: FAIL — `django.db.utils.ProgrammingError: schema "absurd" does not exist` from
cleanup, `psycopg.errors.InvalidSchemaName` from flush.

- [ ] **Step 2: Classify in the cleanup path**

`cleanup.py`'s `cleanup_queues` runs `select ... from absurd.cleanup_all_queues(%s)` on
a Django cursor, so an absent schema arrives as `django.db.utils.ProgrammingError`
wrapping the psycopg error. Wrap the cursor block: catch `ProgrammingError`, raise
`SchemaNotInstalledError` chained `from exc` ONLY when the wrapped cause is
`psycopg.errors.InvalidSchemaName` or `psycopg.errors.UndefinedFunction`; re-raise the
original untouched otherwise. `names_a_queue_table` in `queues.py` is the worked example
of classify-then-chain.

- [ ] **Step 3: Classify in the flush path**

`absurd_flush` calls `client.list_queues()` directly, on the SDK's own cursor, so the
error arrives as a RAW psycopg error that never passes through Django's wrapper — which
is why `flush.py`'s existing tolerant
`except (OperationalError, ProgrammingError, ImproperlyConfigured)` in `clear_queues`
never sees it. Leave `clear_queues` alone: it backs the automatic test cleanup and its
tolerance is deliberate.

Add `list_provisioned_queues` to `queues.py`, below `get_absurd_client`. It builds the
client, returns `sorted(client.list_queues())`, and translates
`psycopg.errors.InvalidSchemaName` / `UndefinedFunction` / `UndefinedTable` into
`SchemaNotInstalledError` chained `from exc`. Point `absurd_flush` at it, dropping its
own `get_absurd_client` + `sorted(...)` lines.

- [ ] **Step 4: Run the command tests**

```bash
uv run pytest tests/core/test_command_errors.py -q --no-cov
```

Expected: all five cases PASS.

- [ ] **Step 5: Run the suites that exercise these paths**

```bash
uv run pytest tests/core/test_cleanup.py tests/core/test_pytest_plugin.py \
              tests/core/test_absurd_fixture.py -q --no-cov
uv run pytest tests/pg_cron/test_flush_scoped.py tests/pg_cron/test_cleanup_schedule.py -q --no-cov
```

Expected: PASS. The flush-related ones prove the automatic test-cleanup path still
tolerates an absent schema — a `SchemaNotInstalledError` escaping `flush_absurd_state`
would break every suite's teardown, so treat a failure here as this task's own bug.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/cleanup.py django_absurd/queues.py \
        django_absurd/management/commands/absurd_flush.py \
        tests/core/test_command_errors.py
git commit -m 'fix: report an unmigrated database from absurd_cleanup and absurd_flush'
```

---

### Task 4: docs

**Files:**

- Modify: `django_absurd/AGENTS.md`
- Modify: `docs/web/configuration.md`

- [ ] **Step 1: Update the integration guide**

In `django_absurd/AGENTS.md`:

- Add `SchemaNotInstalledError` to the exception table, described as the Absurd schema
  being absent, resolved by `manage.py migrate`.
- Replace the bullet reading "The `absurd_worker` / `absurd_beat` / `absurd_sync_crons`
  commands translate `BackendNotConfiguredError` into a `CommandError`" with the base's
  actual contract: every `absurd_*` command turns a configuration failure —
  `ImproperlyConfigured` or any `DjangoAbsurdError` — into a `CommandError`, and
  `--traceback` still shows the original.
- Amend the "hierarchy is not total" bullet: schema-absent is now typed, so it comes off
  the list of conditions that still raise plain `ImproperlyConfigured`.

- [ ] **Step 2: Update the documentation site**

Mirror all three edits in `docs/web/configuration.md`'s exceptions section.

- [ ] **Step 3: Verify the tables render**

```bash
uv run pre-commit run --files django_absurd/AGENTS.md docs/web/configuration.md
```

Then re-read both tables AFTER the hook runs — prettier reflows markdown tables, and a
long cell can lose trailing syntax to a wrap.

- [ ] **Step 4: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/configuration.md
git commit -m 'docs: document the command-error contract and SchemaNotInstalledError'
```

---

## Final gate (coordinator)

```bash
uvx --with tox-uv tox -e dev   # once, here only
uv run pre-commit run --all-files
```

Both green before the branch is offered for review.
