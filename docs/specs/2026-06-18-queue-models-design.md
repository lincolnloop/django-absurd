# django-absurd — Spec 2: Queue model + queue sync command

Date: 2026-06-18 Status: approved-for-planning

## Context

Builds on spec 1 (initial migration installs the `absurd` schema). Spec 2 = ORM
representation of the static queue registry + a declarative queue-sync management
command (`absurd_sync_queues` upserts declared queues; a system check tells you when to
run it; no `migrate` magic). Default DB only.

Discovery facts (verified against a migrated DB):

- Only `absurd.queues` is **static**. The per-queue task/run/checkpoint/event/wait
  tables (`t_/r_/c_/e_/w_/i_<queue>`) are **dynamic** — created by
  `absurd.create_queue()` at runtime, names embed the queue name. NOT modeled in spec 2.
- `absurd.create_queue(name)` is **idempotent** (re-call is a no-op); overloads `(name)`
  and `(name, storage_mode)`.
- SDK ships
  `absurd_sdk.Absurd.create_queue(name, *, storage_mode=…, partition_lookahead=…, cleanup_ttl=…, detach_mode=…, …)`.
  `Absurd(conn_or_url)` accepts an existing psycopg `Connection` — so we reuse Django's
  connection.

## Component 1: read-only `Queue` model

`django_absurd/models.py`:

- `Queue(models.Model)` — `managed = False`, `db_table = 'absurd"."queues'` (the
  embedded `"."` schema-qualifies; no `search_path` change). `queue_name` =
  `primary_key=True`.
- Fields (from `inspectdb`): `queue_name` TextField PK; `created_at` DateTimeField;
  `storage_mode` / `default_partition` / `detach_mode` TextField **+ `TextChoices`**
  matching the SQL CHECK constraints (`unpartitioned|partitioned`, `enabled|disabled`,
  `none|empty`); `partition_lookahead` / `partition_lookback` / `cleanup_ttl` /
  `detach_min_age` DurationField; `cleanup_limit` IntegerField. `__str__` →
  `queue_name`.
- **Read-only:** `save()` and `delete()` raise a clear error pointing to the
  provisioning setting / SDK (a plain ORM insert would write only the registry row and
  skip the per-queue tables `create_queue` builds → a broken queue). Bulk
  `QuerySet.update()/delete()` bypass these — documented caveat, NOT intercepted in
  spec 2.
- The table is created by spec 1's `0001` migration; `managed=False` means Django never
  issues DDL for it. Works on dev + test DB (the migration provides the table —
  spike-proven).

Migration: the Queue `CreateModel(managed=False)` op is **added to the existing
`0001_initial_0_4_0` migration** (alongside its `RunSQL`) — NOT a separate `0002`
migration. Safe because django-absurd is pre-release (no remote, nobody has applied
`0001` downstream) and a `managed=False` `CreateModel` emits **no DDL** (pure model
state; Django rebuilds project state from the file regardless of the applied record).
Keeps `makemigrations --check` clean. Caveat (record for the migration-maintenance
spec): `0001` is the codegen bootstrap, so the codegen must never clobber this
hand-added op — `0001` is frozen, so in practice a non-issue.

## Component 2: declarative queue sync via management command

`ABSURD_QUEUES` is the declarative source of truth. **All mutation happens through an
explicit management command — NO `migrate`/`post_migrate`/`ready()` magic.** `migrate`
only migrates. A system check tells you when to run the command.

### Setting

`ABSURD_QUEUES` — dict `{name: {options}}`, default `{"default": {}}`. Options are
`create_queue` kwargs: `storage_mode` (creation-only), `partition_lookahead`,
`partition_lookback`, `cleanup_ttl`, `cleanup_limit`, `detach_mode`, `detach_min_age`.
Empty dict = defaults. A bare `list[str]` of names is accepted as shorthand (normalized
to `{name: {}}`). `{}` / `[]` = no declared queues.

**Typing:** the SDK already exports `CreateQueueOptions` (and `QueuePolicyOptions`)
TypedDicts — reuse them. Define the setting type as
`AbsurdQueues = Mapping[str, CreateQueueOptions]` (with the `list[str]` shorthand
normalized to that internally). This makes `ABSURD_QUEUES` mypy-checked against the real
SDK option names/types — no hand-maintained duplicate. Our reader normalizes + returns
the typed mapping; the command/check consume it.

### `manage.py absurd_sync_queues` — the only mutation path

Full upsert via the SDK on Django's connection (`Absurd(connections[db].connection)`,
`ensure_connection()` first; `--database` option, default `default`). For each declared
queue:

- **missing** → `create_queue(name, **options)` (incl. `storage_mode`).
- **exists** → `set_queue_policy(name, **mutable_options)` reconcile (all options except
  `storage_mode`). Idempotent — same values = no-op effect.
- **`storage_mode` differs** on an existing queue → report a warning and leave it; it's
  creation-only (reconcile = manual drop+recreate; out of scope).

**Non-destructive:** additive/reconciling only. Queues in the DB but absent from
`ABSURD_QUEUES` are **left alone** (never dropped). Removing a queue is manual (SDK
`drop_queue`). Command prints what it created/reconciled. Idempotent.

### System check — tells you to run it (untagged + guarded)

A registered system check (NOT `Tags.database`) so it runs on ordinary
`check`/`runserver` startup → the nudge appears where users look (mirrors Django's own
unapplied-migrations warning). It always points at the command when queues are declared
but not in sync. Three states (only when queues are declared):

- **DB unreachable** (can't connect) → **skip silently** (no warning) — keeps `check`
  green in no-DB CI; we can't know the state.
- **DB reachable, `absurd` schema absent** (not migrated yet) →
  `W: django-absurd: run 'migrate' then 'manage.py absurd_sync_queues' to provision declared queues`.
  Surfaces the command + the migrate-first prerequisite (you must migrate before the
  sync routine can run).
- **Schema present, drift** (declared queue missing / mutable option ≠ actual /
  `storage_mode` mismatch, flagged creation-only) →
  `W: run 'manage.py absurd_sync_queues'`.
- **Schema present, in sync** → silent.

Reading actual config is **cheap**: `absurd.queues` stores every queue's full policy
(storage_mode + all retention/partition/detach fields), so one guarded query
(`Queue.objects.all()` / SDK `list_queues()`) returns all current config — `queues` is
one row per queue. Wrap in try/except so a connection failure → skip (never error). Not
`app.ready()` (runs everywhere, DB-unsafe, schema may be absent).

### Manual / advanced

The SDK's `create_queue` / `set_queue_policy` / `get_queue_policy` / `drop_queue` /
`list_queues` stay available — users can manage queues directly per the SDK docs; this
layer is convenience, not a cage.

## Testing (pytest, function-based, real Postgres)

1. **Model maps (via the normal flow — set settings, sync, assert):**
   `settings.ABSURD_QUEUES = {"x": {"storage_mode": "partitioned", "cleanup_ttl": "90 days"}}`,
   `call_command("absurd_sync_queues")`, then `Queue.objects.get(queue_name="x")` has
   the mapped field values (`storage_mode == "partitioned"`, `cleanup_ttl` parsed to a
   `timedelta`). No raw `create_queue` in the test — exercise it as users would.
2. **Read-only:** instance `.save()` and `.delete()` raise.
3. **No migrate magic:** with `ABSURD_QUEUES = {"alpha": {}}`, `call_command("migrate")`
   alone creates **no** queue (`Queue.objects.filter(queue_name="alpha").exists()` is
   False).
4. **`absurd_sync_queues` creates:**
   `ABSURD_QUEUES = {"alpha": {}, "beta": {"storage_mode": "partitioned"}}`,
   `call_command("absurd_sync_queues")`; assert both exist, `t_alpha`/`t_beta` exist,
   `beta.storage_mode == "partitioned"`. List shorthand `["alpha"]` → `{"alpha": {}}`.
   `{}`/`[]` → nothing.
5. **`absurd_sync_queues` reconciles (upsert):** with `q` already created, change its
   declared option in `settings.ABSURD_QUEUES` (e.g. `cleanup_ttl` to a new value), run
   `call_command("absurd_sync_queues")` → `Queue.objects.get("q").cleanup_ttl` (and
   `get_queue_policy`) reflect the new value. Second run idempotent (no change).
6. **Non-destructive:** a queue absent from `ABSURD_QUEUES` survives the command (not
   dropped). `storage_mode` change on an existing queue is reported, not applied.
7. **System check (three states):** schema present + drift → warning naming
   `absurd_sync_queues`; schema present + in-sync → no warning; DB reachable + `absurd`
   schema absent → warning telling you to `migrate` then `absurd_sync_queues`; DB
   unreachable → no warning (no error).
8. `makemigrations --check` clean (Queue's `CreateModel(managed=False)` op lives in
   `0001`; no stray migration generated).

## Out of scope

Per-queue task/run model factory; Tasks API; workers. Queue removal (manual via SDK
`drop_queue`); `storage_mode` change on an existing queue (drop+recreate — not
automated).

## Deferred / TODO

- **Cleanup / retention mechanism.** Absurd ages out completed/failed/cancelled tasks +
  events via SQL functions `absurd.cleanup_all_queues`, `absurd.cleanup_tasks`,
  `absurd.cleanup_events` (per-queue `cleanup_ttl`/`cleanup_limit`). The **SDK exposes
  NO cleanup method**, and `absurdctl` (which has a `cleanup` command) is dev-only / not
  shippable. So the likely django-absurd mechanism is a thin **management command**
  (and/or scheduled hook) wrapping those SQL functions — don't reinvent, wrap what
  exists. Decide SDK-vs-SQL-vs-command in its own brainstorm. (SDK also has no cleanup
  but does have `drop_queue`/`list_queues`/ `get_queue_policy`/`set_queue_policy` for
  later queue management.)
- **Per-queue read-model factory** (`t_/r_/…<queue>`) — pair with its consumer when
  binding the Tasks API (spec 3).
- **`storage_mode` change reconciliation** — the check warns; automating drop+recreate
  (data loss) is intentionally out of scope.
