# Command error translation — design

Closes [#128](https://github.com/lincolnloop/django-absurd/issues/128).

## Problem

Six management commands, two bases. `absurd_sync_queues` and `absurd_worker` inherit
`AbsurdReportCommand`; `absurd_beat`, `absurd_cleanup`, `absurd_flush` and
`absurd_sync_crons` inherit `BaseCommand`. `AbsurdReportCommand` only reports a
`SyncResult` to stdout — it never touches `handle`, so error translation is per-command
and uneven:

- `ImproperlyConfigured` → `CommandError` in exactly one place, wrapping
  `provision_backend` in `absurd_worker`.
- `BackendNotConfiguredError` → `CommandError` hand-rolled three times, in
  `absurd_beat`, `absurd_worker`, `absurd_sync_crons`.

Measured against an unmigrated database (2026-08-15, all six commands, three configs):

| command              | unmigrated DB                                  | 0 backends      | 2 backends    |
| -------------------- | ---------------------------------------------- | --------------- | ------------- |
| `absurd_sync_queues` | traceback — `ImproperlyConfigured`             | message, exit 0 | `absurd.E004` |
| `absurd_cleanup`     | traceback — `django.db.utils.ProgrammingError` | message, exit 0 | `absurd.E004` |
| `absurd_flush`       | traceback — `psycopg.errors.InvalidSchemaName` | message, exit 0 | `absurd.E004` |
| `absurd_beat`        | fine — touches no DB at start                  | `CommandError`  | `absurd.E004` |
| `absurd_worker`      | `CommandError`                                 | `CommandError`  | `absurd.E004` |

Two findings redirect the issue as filed:

1. **The `BackendNotConfiguredError` half buys no user-visible improvement.** The
   multiple-backend count is unreachable from a command — `absurd.E004` fires in system
   checks first — and the zero count already reports cleanly everywhere. Worth doing as
   dedup, not as a fix.
2. **The `ImproperlyConfigured` half alone fixes only `absurd_sync_queues`.** The two
   commands that leak worst raise raw psycopg / `ProgrammingError`, which a base
   catching `ImproperlyConfigured` never sees. The issue's guess that the other five
   commands are the wins is wrong: `absurd_beat` was never broken.

So a base class alone leaves the ugliest failures ugly. The schema probe has to become a
typed error at the same time.

## Ship

### One base

```
AbsurdCommand(BaseCommand)               # overrides execute()
└── AbsurdReportCommand(AbsurdCommand)   # keeps report_sync_result
```

`AbsurdCommand.execute` wraps `super().execute()`, catches `ImproperlyConfigured` and
`DjangoAbsurdError`, re-raises `CommandError` chained `from exc`. All six commands
inherit it; the four hand-rolled translations go.

Overriding `execute` rather than `handle`: one override, no command renames its
`handle`, and the system-check phase is covered too. Nothing is lost — Django's
`--traceback` still prints the original chain, and `CommandError` is what Django's own
commands raise.

`call_command` runs through `execute` as well, so programmatic callers see
`CommandError` too. That is the same contract Django's built-in commands offer, and
three existing tests that assert the pre-translation type move to the new one.

Alternative dropped: a report mixin beside the base. Two levels of plain inheritance is
less machinery for the same result when one method is shared.

### One typed schema error

`SchemaNotInstalledError(DjangoAbsurdError)`, no constructor arguments, owning the
message `Absurd schema is not installed. Run: manage.py migrate`.

Raised wherever the absent schema is probed:

- the three sites hand-rolling `ImproperlyConfigured` today — queue reconcile, the
  enqueue path, the worker's client probe. Two different wordings collapse to one; the
  worker's `then manage.py absurd_sync_queues` tail is redundant because `post_migrate`
  provisions declared queues.
- new probes in the cleanup and flush paths, which raise raw psycopg errors today. Each
  classifies only the errors that name a missing Absurd object, chains `from exc`, and
  re-raises anything else untouched — narrow catch, per the exception-chaining
  convention.

The enqueue path switches type too, so one condition has one type everywhere. That is a
break for anyone catching `ImproperlyConfigured` around `enqueue`, and
`DjangoAbsurdError` deliberately does not subclass it, so there is no soft landing. The
commit is `feat!` and names the change.

### After

Every command on an unmigrated database:

```
CommandError: Absurd schema is not installed. Run: manage.py migrate
```

exit 1, no traceback. In a web process the enqueue path still raises, still with a
traceback, but the type names the condition instead of surfacing as a psycopg internal.

## Out of scope

- **The exit-code split.** `absurd_cleanup` / `absurd_flush` print
  `No Absurd task backends configured.` and exit 0; `absurd_beat` / `absurd_worker` /
  `absurd_sync_crons` raise `CommandError` and exit 1. Defensible — an idempotent
  maintenance no-op versus a process that cannot start — and undocumented. Leave both,
  note it if the docs sweep finds a place for it.
- Broadening translation beyond configuration failures. Task errors inside a running
  worker keep their current handling.

## Testing

Integration only, through real entrypoints — `call_command` under the existing
schema-hiding helper, one case per command, asserting the complete `CommandError` text.
No test calls the base class or the probe helpers directly.

The three existing assertions on the schema condition change type. The check that
`migrate` refuses a non-psycopg3 backend is a different condition and keeps
`ImproperlyConfigured`.

## Docs

- The exception table and the "commands translate `BackendNotConfiguredError`" bullet in
  the integration guide, which becomes the base's contract.
- The same table on the documentation site.
- The honesty note about the hierarchy not being total: schema-absent moves out of the
  "still raises plain `ImproperlyConfigured`" list.
