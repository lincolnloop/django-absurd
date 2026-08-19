# Refuse partitioned queues — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement task-by-task. Steps use checkbox (`- [ ]`)
> syntax.

**Goal:** Make `storage_mode="partitioned"` a configuration error, and delete the
partitioned-specific handling rather than carry it.

**Architecture:** One static refusal at the declaration door (`absurd.E003`). No runtime
guard — `absurd.create_queue` already raises
`Queue "x" already exists with storage mode "partitioned"` on any attempt to re-create
an out-of-band partitioned queue as unpartitioned, so Postgres names the condition
without us duplicating pinned policy. Everything else is deletion.

**Tech stack:** Django 6.0+/Python 3.12+, psycopg3, pytest (function-based), mypy
strict.

**Tracking issue:** <https://github.com/lincolnloop/django-absurd/issues/216> — carries
the recovery SHA, the SDK's pg_cron lifecycle design, and the `i_<queue>` finding.

## Global constraints

- Recovery point for deleted code: `6b2de4270872d1b4e7038a54e7c79ebdfec99226`.
- Hint text references the issue by **full URL**, never bare `#216`.
- Check convention: `msg` states the problem, `hint` states the resolution. No fix text
  in both.
- Tests function-based, never class-based. Assert literal check msg/hint text inline;
  never import the constants.
- Run scoped: `uv run pytest <path> -v -n4`. Before commit:
  `uvx --with tox-uv tox -e dev` + `uv run pre-commit run --all-files`.
- 100% statement+branch coverage on changed lines. No `pragma: no cover`, no ruff
  ignores.
- Breaking change → commit subject is `feat!:`, never `refactor!`/`chore!`.

## Why deletion, not preservation

Most of the partitioned handling is coupled to the broken behaviour.
`test_sync_leaves_a_provisioned_partitioned_queue_alone` exists only because
`ensure_partitions` raises `CheckViolation` past the window — premise disappears the
moment a lifecycle lands. Repair semantics shift too. Keeping it means maintaining tests
for a scenario no supported configuration produces.

One finding is durable and already recorded on the issue: `i_<queue>` is a partitioned
queue's sixth table, and `absurd.spawn_task` reserves the idempotency key there before
touching `t_<queue>`.

## What stays, deliberately

- `Queue.StorageMode` choices + `partition_lookahead` / `partition_lookback` /
  `default_partition` model fields — pure db reads for the admin, nothing to do with
  declaration.
- `flush.truncate_queue_tables`'s `to_regclass('absurd.i_<queue>')` probe.
  `clear_queues` iterates `list_provisioned_queues()` (db catalog, not declarations), so
  it is the one path reaching a queue we never declared. Mode-agnostic by construction —
  "truncate it if it is there". Deleting it buys nothing and opens the only real
  correctness hole.
- `VALID_STORAGE_MODES` — still rejects a genuinely invalid literal like `"bogus"`.
  `"partitioned"` passes that gate and is caught by the new branch after it.

## File structure

| File                                                      | Change                                                                                                                                                                                                                                |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `django_absurd/checks.py`                                 | Add the refusal. Delete `W002_MSG`/`W002_HINT`, `query_queue_state`, `check_absurd_queue_state`.                                                                                                                                      |
| `django_absurd/queues.py`                                 | Delete `QUEUE_OWNED_TABLE_PREFIXES`, `find_missing_queue_tables`'s `storage_mode` kwarg, `afind_missing_queue_tables`'s SQL `case when`, `QueuePlan.repair_storage_mode`, `QueuePlan.storage_warning`, `SyncResult.storage_warnings`. |
| `django_absurd/management/commands/absurd_sync_queues.py` | Delete the stderr branch, `stderr_prefix`, and the `crate_err` glyph prefix.                                                                                                                                                          |
| `django_absurd/apps.py`                                   | Delete the `storage_warnings` line from the post-migrate summary.                                                                                                                                                                     |
| `docs/web/configuration.md`                               | Replace the experimental bullet; drop the `absurd.W002` table row.                                                                                                                                                                    |
| `django_absurd/AGENTS.md`                                 | Same two edits.                                                                                                                                                                                                                       |
| `docs/WHY.md`                                             | Rewrite the partitioned entry under "Deliberately not doing".                                                                                                                                                                         |
| 9 test files                                              | Delete 7 partition-specific tests; convert 8 foil sites.                                                                                                                                                                              |

---

### Task 1: Refuse the declaration

**Files:**

- Modify: `django_absurd/checks.py` (message constants near `E003_HINT`;
  `validate_queue_policy`)
- Test: `tests/core/test_checks.py`

**Interfaces:**

- Produces: a new `Error` under id `absurd.E003` for `storage_mode="partitioned"` and
  for each of `partition_lookahead`, `partition_lookback`, `detach_mode`,
  `detach_min_age`.
- `cleanup_ttl` / `cleanup_limit` keep working — they apply to both storage modes.

- [ ] **Step 1: write the failing tests**

```python
def test_declaring_partitioned_storage_is_an_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = build_tasks_setting({"q": {"storage_mode": "partitioned"}})
    out = run_absurd_check(capsys)
    assert "absurd.E003" in out
    assert (
        "django-absurd: partitioned queues are not supported."
        " Queue 'q' declares storage_mode 'partitioned'." in out
    )
    assert (
        "Declare storage_mode 'unpartitioned', or omit it. Track partitioned"
        " support at https://github.com/lincolnloop/django-absurd/issues/216." in out
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("partition_lookahead", "28 days"),
        ("partition_lookback", "7 days"),
        ("detach_mode", "empty"),
        ("detach_min_age", "30 days"),
    ],
)
def test_declaring_a_partition_only_policy_key_is_an_error(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    key: str,
    value: str,
) -> None:
    settings.TASKS = build_tasks_setting(
        t.cast("dict[str, CreateQueueOptions]", {"q": {key: value}})
    )
    out = run_absurd_check(capsys)
    assert "absurd.E003" in out
    assert f"Queue 'q' declares '{key}'" in out


def test_retention_policy_keys_are_still_accepted(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = build_tasks_setting(
        {"q": {"cleanup_ttl": "7 days", "cleanup_limit": 100}}
    )
    assert "absurd.E003" not in run_absurd_check(capsys)
```

- [ ] **Step 2: run them, confirm RED**

`uv run pytest tests/core/test_checks.py -v -n4`. Expect failures on the two new error
cases (no such error raised); the retention test passes already.

- [ ] **Step 3: implement**

Two message constants beside `E003_HINT`, one for the storage mode and one for the
partition-only keys, plus a shared hint carrying the issue URL. Branch in
`validate_queue_policy`, after the existing `VALID_STORAGE_MODES` gate so a bogus
literal still reports as invalid rather than as unsupported. Iterate the four
partition-only key names in declaration order so output is stable.

- [ ] **Step 4: confirm GREEN**

`uv run pytest tests/core/test_checks.py -v -n4`.

- [ ] **Step 5: commit**

`feat!: refuse storage_mode="partitioned" as a configuration error`

---

### Task 2: Clear partitioned declarations out of the suite

Task 1 turns 15 declaration sites into check errors. Every management command runs
non-database checks, so those tests fail until this lands. Delete what tested the broken
behaviour; convert what used partitioned only as a foil.

**Files:**

- Delete tests: `tests/core/test_queue_sync.py:317,347,370`,
  `tests/core/test_enqueue.py:128`, `tests/core/test_worker.py:345`,
  `tests/core/test_pytest_plugin.py:53`, `tests/core/test_admin/test_task.py:315`
- Modify: `tests/core/test_backend.py:38`, `tests/core/test_command_output.py:80`,
  `tests/core/test_checks.py:92`, `tests/core/test_queue_sync.py:242,469,489`,
  `tests/multidb/test_check.py:22`

- [ ] **Step 1: delete the seven partition-specific tests**

Each one exercises code Task 3 deletes.
`test_sync_leaves_a_provisioned_partitioned_queue_alone` in particular pins "do not
touch it, because `ensure_partitions` would raise" — the behaviour the tracking issue
exists to fix.

- [ ] **Step 2: convert the foil sites**

`test_backend.py:38` uses `{"storage_mode": "partitioned"}` only to prove two queues
parse — swap for `{}`. `test_queue_sync.py:242` asserts policy round-trips — keep
`cleanup_ttl`, drop `storage_mode`, and assert on the retained policy instead of
`q.storage_mode`.

The four drift sites (`test_command_output.py:80`, `test_checks.py:92`,
`test_queue_sync.py:469,489`, `multidb/test_check.py:22`) test `W002` and its sync-time
twin. Both die in Task 3 — delete these along with them.

- [ ] **Step 3: delete the stderr-encoding test**

`test_sync_queues_probes_the_warning_glyph_against_stderr_not_stdout`
(`test_command_output.py:80`) uses the drift warning as its vehicle, and it is the only
thing writing to this command's stderr.
`test_sync_queues_drops_the_crate_on_a_stream_that_cannot_encode_it` (`:68`) still
covers the same hazard on stdout.

- [ ] **Step 4: run all three suites**

`uv run pytest tests/core -n4`, `uv run pytest tests/pg_cron -n4`,
`uv run pytest tests/multidb -n4`. Expect green except the deletions Task 3 makes — if
anything else fails, a foil site was missed.

- [ ] **Step 5: commit**

`test: drop partitioned queue declarations from the suite`

---

### Task 3: Delete the partitioned handling

**Files:**

- Modify: `django_absurd/queues.py`, `django_absurd/checks.py`,
  `django_absurd/management/commands/absurd_sync_queues.py`, `django_absurd/apps.py`

- [ ] **Step 1: collapse the table-prefix sets**

`QUEUE_OWNED_TABLE_PREFIXES` and its comment go; `QUEUE_TABLE_PREFIXES` is the only set
left. `names_a_queue_table` iterates it instead.

- [ ] **Step 2: simplify both missing-table probes**

`find_missing_queue_tables` loses its keyword-only `storage_mode` parameter and the
prefix branch; update both call sites. `afind_missing_queue_tables` loses the
`case when q.storage_mode = 'partitioned'` and its two-array parameter dict, becoming a
single `unnest` over one array. Keep the twin docstring's "change one and change the
other" note — the two still have to agree.

- [ ] **Step 3: simplify the plan**

`QueuePlan.repair_storage_mode` becomes a plain `repair: bool` — repair now calls
`create_queue` with no storage mode. Delete `QueuePlan.storage_warning`,
`SyncResult.storage_warnings`, and their branches in `plan_queue`, `apply_queue_plan`,
`summarize_queue_plans`, and `sync_queues`.

Note in the repair comment why the gate on absent tables survives the deletion:
unconditional re-creation would still be needless DDL, and `absurd.create_queue` raises
`Queue "x" already exists with storage mode "partitioned"` against an out-of-band
partitioned row — a loud, accurate failure we do not curate.

- [ ] **Step 4: delete the stderr path**

In `absurd_sync_queues`, drop the `crate_err` glyph prefix, the `stderr_prefix`
parameter and its two call sites, and the `for warning in result.storage_warnings` loop.
In `apps.py`, drop the `storage_warnings` line from the post-migrate summary.

- [ ] **Step 5: delete W002**

`W002_MSG`, `W002_HINT`, `query_queue_state`, and `check_absurd_queue_state` all go —
the last is the only `Tags.database` check, and W002 was its only output.

- [ ] **Step 6: run the full gate**

`uvx --with tox-uv tox -e dev`, then `uv run pre-commit run --all-files` (owns mypy
strict). Check the coverage report for newly-unreached lines — an orphan is a deletion
that was missed.

- [ ] **Step 7: commit**

`feat!: drop the partitioned-queue handling and absurd.W002`

---

### Task 4: Documentation

**Files:**

- Modify: `docs/web/configuration.md:50-51,107`,
  `django_absurd/AGENTS.md:1187-1190,1246`, `docs/WHY.md`

- [ ] **Step 1: rewrite the queue-policy caveat in both docs homes**

Replace the "experimental — not tested yet" bullet with a statement that `partitioned`
is refused as a configuration error, linking the issue. Tier-1 caveat: one line under
the concept it belongs to. In `AGENTS.md` the same fact is bold prose, not an
admonition.

Both files also carry a check table with an `absurd.W002` row — delete it, and check
whether the surviving `storage_mode`-is-immutable line still says anything true once the
mode cannot be declared.

- [ ] **Step 2: rewrite the WHY.md entry**

The "Deliberately not doing" entry currently says partitioned queues "can be declared"
and the scope decision "is tracked separately". Both are now wrong. Record the decision
and the reason that outlives it: an option whose failure mode is silent data
concentration a month after you set it is worse than no option. Keep it at the why
altitude — no file names, no structure map. Say nothing narrowing about `CLEANUP`'s
scope: folding partition upkeep into that same job is the natural shape if support
lands, and describing it as row-retention-only today reads as a contract change later.

- [ ] **Step 3: build the site**

`uvx zensical build` — expect "No issues found". No `nav` change; no page added or
renamed.

- [ ] **Step 4: cross-check**

Grep both docs homes for `partition` and `storage_mode`. Command names, flag names and
message text must match `checks.py` verbatim.

- [ ] **Step 5: commit**

`docs: partitioned queues are refused, not experimental`

---

## Self-review

- **Coverage.** Refusal → Task 1. Deletion → Task 3. Test fallout → Task 2. Docs →
  Task 4. Issue/recovery-SHA → already published on the tracking issue.
- **Ordering.** Task 1 breaks 15 test sites; Task 2 must follow immediately or the tree
  is red between commits. Task 3 cannot precede Task 2 — the tests would fail on missing
  attributes rather than on the check.
- **Naming.** `repair_storage_mode` → `repair` is the only rename; it is referenced in
  `plan_queue`, `apply_queue_plan` and `summarize_queue_plans`, all inside Task 3.
- **Not done here, on purpose.** No runtime guard, no `Tags.database` refusal for a
  provisioned partitioned queue. Postgres already raises an accurate error, and a
  curated type would duplicate policy against pinned SQL that we do not own.
