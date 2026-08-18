# Document history

Specs and plans retired once their work shipped (and their durable "why" was captured in
[`WHY.md`](WHY.md)). Each line links to the full original, frozen at the `origin/main`
commit where it last lived — recoverable from git any time.

## Specs

- 2026-06-17 — migration-wrapping: ship Absurd's schema as offline Django migrations
  wrapping the pinned `absurdctl` SQL →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-17-migration-wrapping-design.md)
- 2026-06-18 — queue-models: read-only Queue model + non-destructive queue sync (early
  `ABSURD_QUEUES` setting, later moved to `TASKS`) →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-18-queue-models-design.md)
- 2026-06-19 — configurable-absurd-database: per-backend database selection + router
  (early `ABSURD_DATABASE` setting, later moved to `TASKS` OPTIONS) →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-19-configurable-absurd-database-design.md)
- 2026-06-19 — host-dev-tox-matrix: host-based dev/test via uv/tox (early Django 5.2 /
  py3.10–3.11 matrix) →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-19-host-dev-tox-matrix-design.md)
- 2026-06-19 — tasks-api-config-migration: move configuration onto Django's `TASKS`
  setting, dropping the `ABSURD_*` settings →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-19-tasks-api-config-migration-design.md)
- 2026-06-19 — tasks-api-enqueue: enqueue on Django's connection inside the caller's
  transaction (enqueue-on-commit) →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-19-tasks-api-enqueue-design.md)
- 2026-06-22 — tasks-api-lazy-discovery: resolve task callables lazily by import path,
  replacing eager autodiscovery/registration →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-22-tasks-api-lazy-discovery-design.md)
- 2026-06-22 — tasks-api-result-retrieval: `get_result` via queue-scoped id +
  cursor-scoped jsonb loader →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-22-tasks-api-result-retrieval-design.md)
- 2026-06-22 — tasks-api-spawn-options: per-task and per-call Absurd params
  (max_attempts, retry, cancellation, headers, idempotency) →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-22-tasks-api-spawn-options-design.md)
- 2026-06-22 — tasks-api-worker: worker with a dedicated autocommit connection (original
  eager-discovery half superseded by lazy-discovery) →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/specs/2026-06-22-tasks-api-worker-design.md)
- 2026-06-17 — migration-maintenance: regenerate the wrapped Absurd schema offline with
  `absurdctl` when bumping the pinned version →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-06-17-migration-maintenance-design.md)
- 2026-06-24 — async-worker: worker runs async handlers + the async SDK on its own
  dedicated autocommit connection →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-06-24-async-worker-design.md)
- 2026-06-24 — auto-create-queues: declared queues provisioned at migrate/sync and
  auto-created on first enqueue →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-06-24-auto-create-queues-design.md)
- 2026-06-24 — dream-knowledge-distillation: the capture-why / archive-specs doc
  distillation tooling (delivered as project skills) →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-06-24-dream-knowledge-distillation-design.md)
- 2026-06-25 — admin-queue-introspection: read-only Django admin over per-queue
  UNION-ALL views →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-06-25-admin-queue-introspection-design.md)
- 2026-06-25 — orm-queue-table-access: read-only ORM models backed by the same UNION-ALL
  queue views →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-06-25-orm-queue-table-access-design.md)
- 2026-06-29 — scheduler: application-side beat scheduler (settings `SCHEDULE`,
  fire-forward, per-slot idempotency) →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-06-29-scheduler-design.md)
- 2026-07-03 — pgcron-scheduler: database-side `pg_cron` scheduler (opt-in app,
  `ScheduledTask` projection + constant-command wrapper) →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-07-03-pgcron-scheduler-design.md)
- 2026-07-07 — examples-nanodjango-three-apps: three single-file nanodjango demos (web /
  beat / pg_cron) →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-07-07-examples-nanodjango-three-apps-design.md)
- 2026-07-07 — pgcron-schedule-admin: read-only `ScheduledTask` admin (changelist +
  fieldsets, "Absurd Cron" section) →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/specs/2026-07-07-pgcron-schedule-admin-design.md)
- 2026-07-07 — admin-definable-schedules: admin-authored `pg_cron` schedule store,
  source-namespaced with the settings lane kept read-only →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-07-admin-definable-schedules-design.md)
- 2026-07-15 — cleanup-task: retention deletion of aged task/event history per queue
  policy →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-15-cleanup-task-design.md)
- 2026-07-15 — two-step-scheduledtask-admin: two-step create flow for admin-authored
  `ScheduledTask` rows →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-15-two-step-scheduledtask-admin-design.md)
- 2026-07-16 — declarative-cleanup-schedule: `OPTIONS["CLEANUP"]` drives retention under
  beat + a native `pg_cron` cleanup job →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-16-declarative-cleanup-schedule-design.md)
- 2026-07-17 — single-absurd-backend: exactly one `AbsurdBackend` per project, resolved
  by capability not by the `default` name →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-17-single-absurd-backend-design.md)
- 2026-07-18 — durable-steps-and-sleep: checkpointed steps + durable sleep reached via
  an on-demand accessor, not a task-context subclass →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-18-durable-steps-and-sleep-design.md)
- 2026-07-18 — scheduler-from-app-presence: derive scheduler (beat / `pg_cron`) from
  `INSTALLED_APPS`, dropping the `SCHEDULER` option and its check →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-18-scheduler-from-app-presence-design.md)
- 2026-07-19 — events-await-emit: durable `await_event` + top-level `emit_event`
  (`await_task_result` cut) →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-19-events-await-emit-design.md)
- 2026-07-21 — pytest-plugin: automatic Absurd test-state cleanup with Django parity
  (monkeypatch of the post-teardown hook; the fixture approach was superseded) →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-21-pytest-plugin-design.md)
- 2026-07-21 — sync-schedules-on-migrate: test-database-gated automatic `pg_cron` sync
  at migrate time →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/specs/2026-07-21-sync-schedules-on-migrate-design.md)
- 2026-07-23 — pg-cron-test-inert-design: cross-database `pg_cron` scheduling —
  operator-managed extension on the central `cron.database_name`, jobs bound to the app
  DB via `schedule_in_database` with db-namespaced jobnames (upsert-steal defense), and
  a seam that stays inert under tests unless opted in →
  [view @4dde493](https://github.com/lincolnloop/django-absurd/blob/4dde493f4a6981f5146ba5feaf5f973ce76103ab/docs/specs/2026-07-23-pg-cron-test-inert-design.md)
- 2026-08-08 — docs-howto-rewrite: the example-first rewrite of the docs site — house
  style, caveat tiers, per-page plan, and dissolving the how-it-works page (style now
  lives in the `sync-docs` skill) →
  [view @b33abe6](https://github.com/lincolnloop/django-absurd/blob/b33abe6ee44454adaa27225cad2310a5fc72a988/docs/specs/2026-08-08-docs-howto-rewrite-design.md)
- 2026-08-08 — static-pg-cron-grammar: replace the live `cron.*` probe with a Python
  grammar matcher, with the measured pg_cron acceptance table behind the deliberate
  divergence →
  [view @b33abe6](https://github.com/lincolnloop/django-absurd/blob/b33abe6ee44454adaa27225cad2310a5fc72a988/docs/specs/2026-08-08-static-pg-cron-grammar.md)
- 2026-08-10 — git-cliff-changelog: generate `CHANGELOG.md` from commit subjects —
  section set, dropping every Renovate commit, the one-time backfill, prepend-only →
  [view @b33abe6](https://github.com/lincolnloop/django-absurd/blob/b33abe6ee44454adaa27225cad2310a5fc72a988/docs/specs/2026-08-10-git-cliff-changelog-design.md)

## Plans

- 2026-06-17 — migration-wrapping implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-17-migration-wrapping.md)
- 2026-06-19 — configurable-absurd-database implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-19-configurable-absurd-database.md)
- 2026-06-19 — host-dev-tox-matrix implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-19-host-dev-tox-matrix.md)
- 2026-06-19 — queue-models implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-19-queue-models.md)
- 2026-06-19 — tasks-api-config-migration implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-19-tasks-api-config-migration.md)
- 2026-06-19 — tasks-api-enqueue implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-19-tasks-api-enqueue.md)
- 2026-06-22 — tasks-api-lazy-discovery implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-22-tasks-api-lazy-discovery.md)
- 2026-06-22 — tasks-api-result-retrieval implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-22-tasks-api-result-retrieval.md)
- 2026-06-22 — tasks-api-spawn-params implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-22-tasks-api-spawn-params.md)
- 2026-06-22 — tasks-api-worker implementation plan →
  [view @67a22ec](https://github.com/lincolnloop/django-absurd/blob/67a22ec1e42708e77bb4b2833039a2839189dbf3/docs/superpowers/plans/2026-06-22-tasks-api-worker.md)
- 2026-06-24 — async-worker implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-06-24-async-worker.md)
- 2026-06-24 — auto-create-queues implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-06-24-auto-create-queues.md)
- 2026-06-24 — dream-knowledge-distillation implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-06-24-dream-knowledge-distillation.md)
- 2026-06-25 — admin-queue-introspection implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-06-25-admin-queue-introspection.md)
- 2026-06-25 — orm-queue-table-access implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-06-25-orm-queue-table-access.md)
- 2026-06-29 — scheduler-beat implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-06-29-scheduler-beat.md)
- 2026-07-07 — examples-nanodjango-three-apps implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-07-07-examples-nanodjango-three-apps.md)
- 2026-07-07 — pgcron-schedule-admin implementation plan →
  [view @912fea3](https://github.com/lincolnloop/django-absurd/blob/912fea398f7b93f41fd520f420841be7dd9232fb/docs/plans/2026-07-07-pgcron-schedule-admin.md)
- 2026-07-07 — admin-definable-schedules phase-a implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-07-admin-definable-schedules-phase-a.md)
- 2026-07-09 — admin-writable-schedules phase-b implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-09-admin-writable-schedules-phase-b.md)
- 2026-07-14 — typed-spawn-options-and-dynamic-queue implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-14-typed-spawn-options-and-dynamic-queue.md)
- 2026-07-15 — two-step-scheduledtask-admin implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-15-two-step-scheduledtask-admin.md)
- 2026-07-16 — cleanup-helper implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-16-cleanup-helper.md)
- 2026-07-16 — declarative-cleanup-schedule implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-16-declarative-cleanup-schedule.md)
- 2026-07-17 — single-absurd-backend implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-17-single-absurd-backend.md)
- 2026-07-18 — durable-steps-and-sleep implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-18-durable-steps-and-sleep.md)
- 2026-07-18 — scheduler-from-app-presence implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-18-scheduler-from-app-presence.md)
- 2026-07-19 — events-await-emit implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-19-events-await-emit.md)
- 2026-07-21 — sync-schedules-on-migrate implementation plan →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-21-sync-schedules-on-migrate.md)
- 2026-07-22 — absurd-db-autodetect implementation plan (abandoned fixture-autodetect
  approach; superseded by the shipped post-teardown monkeypatch) →
  [view @d49eb31](https://github.com/lincolnloop/django-absurd/blob/d49eb31405cd55fbeeea1e601756d0c1c7acf332/docs/plans/2026-07-22-absurd-db-autodetect.md)
- 2026-07-24 — pg-cron-cross-database-scheduling implementation plan (10-task TDD build
  of the inert cross-database `pg_cron` design) →
  [view @4dde493](https://github.com/lincolnloop/django-absurd/blob/4dde493f4a6981f5146ba5feaf5f973ce76103ab/docs/plans/2026-07-24-pg-cron-cross-database-scheduling.md)
- 2026-07-27 — typed-absurd-params design spec (one `absurd_params` factory bound at
  enqueue, replacing the untypeable `absurd_spawn_params=` kwarg; the overload pair
  splitting definition-site from per-invocation fields, and the wrong-door error
  taxonomy) →
  [view @24c7b81](https://github.com/lincolnloop/django-absurd/blob/24c7b8192f42c07bb4d9c1f60fab6c71f72d5010/docs/specs/2026-07-27-typed-absurd-params.md)
- 2026-07-28 — typed-absurd-params implementation plan (TDD build of the spec above) →
  [view @24c7b81](https://github.com/lincolnloop/django-absurd/blob/24c7b8192f42c07bb4d9c1f60fab6c71f72d5010/docs/plans/2026-07-28-typed-absurd-params.md)
- 2026-07-28 — durable-test-clock design spec (the two-clock freeze over Postgres's
  `absurd.fake_now` and time-machine, the per-run and per-task snapshot records, and the
  post-review redesign of the clock into a context manager) →
  [view @24c7b81](https://github.com/lincolnloop/django-absurd/blob/24c7b8192f42c07bb4d9c1f60fab6c71f72d5010/docs/specs/2026-07-28-durable-test-clock-design.md)
- 2026-07-29 — durable-test-clock implementation plan (TDD build of the `dj_absurd`
  fixture, plus the suite-wide migration off freezegun) →
  [view @24c7b81](https://github.com/lincolnloop/django-absurd/blob/24c7b8192f42c07bb4d9c1f60fab6c71f72d5010/docs/plans/2026-07-29-durable-test-clock.md)
- 2026-07-31 — run-after-wrapper-task implementation plan (deferred enqueue via a
  wrapper run, replacing the injected sleep that broke both cancellation rules;
  specifies a `django_absurd:defer:` name _prefix_, where the shipped code uses a
  `:run_after` suffix) →
  [view @24c7b81](https://github.com/lincolnloop/django-absurd/blob/24c7b8192f42c07bb4d9c1f60fab6c71f72d5010/docs/plans/2026-07-31-run-after-wrapper-task.md)
- 2026-08-01 — task-signals design spec (sending Django's three lifecycle signals from
  the backend: the enqueue and worker seams, one containment helper so no receiver can
  damage a task, terminal-only failure reporting, a start per episode rather than per
  attempt, and why Absurd's own control-flow endings announce nothing) →
  [view @8bd0a54](https://github.com/lincolnloop/django-absurd/blob/8bd0a54ace32b774ea708ef9cc6a91588a805c6f/docs/specs/2026-08-01-task-signals-design.md)
- 2026-08-02 — task-signals implementation plan (TDD build of the two seams, the
  containment helper, and the receiver-raises tests) →
  [view @8bd0a54](https://github.com/lincolnloop/django-absurd/blob/8bd0a54ace32b774ea708ef9cc6a91588a805c6f/docs/plans/2026-08-02-task-signals.md)
- 2026-08-06 — worker-concurrency design spec (one worker mode: a local rolling-window
  claim loop replacing the SDK's batch-joining async worker, an own stop event with
  graceful-stop/cancel-and-drain semantics, and removal of `absurd_worker --burst` as
  test scaffolding that had leaked onto the CLI) →
  [view @8bd0a54](https://github.com/lincolnloop/django-absurd/blob/8bd0a54ace32b774ea708ef9cc6a91588a805c6f/docs/specs/2026-08-06-worker-concurrency-design.md)
- 2026-08-06 — worker-concurrency implementation plan (six tasks: refill loop, shutdown
  semantics, the ~50-site test migration off `--burst`, the signal-driven command
  helper, the removal itself, and the docs sweep) →
  [view @8bd0a54](https://github.com/lincolnloop/django-absurd/blob/8bd0a54ace32b774ea708ef9cc6a91588a805c6f/docs/plans/2026-08-06-worker-concurrency.md)
- 2026-08-10 — git-cliff-changelog implementation plan (cliff.toml, backfill over
  `a1..a5`, the release-skill step, and slicing the release body out of the file) →
  [view @b33abe6](https://github.com/lincolnloop/django-absurd/blob/b33abe6ee44454adaa27225cad2310a5fc72a988/docs/plans/2026-08-10-git-cliff-changelog.md)
- 2026-08-15 — command-error-translation design spec (one command base translating a
  narrow set of configuration failures into a clean `CommandError`, and the measured
  six-command behaviour table it was decided from) →
  [view @d852573](https://github.com/lincolnloop/django-absurd/blob/d852573719c117857f8bc07eca46db145b80792c/docs/specs/2026-08-15-command-error-translation-design.md)
- 2026-08-15 — command-error-translation implementation plan (the base class, the
  per-command migration, and the exception messages each command surfaces) →
  [view @d852573](https://github.com/lincolnloop/django-absurd/blob/d852573719c117857f8bc07eca46db145b80792c/docs/plans/2026-08-15-command-error-translation.md)
- 2026-08-17 — explicit-provisioning design spec (provisioning as a deploy step: enqueue
  and the worker refusing an unprovisioned queue, `post_migrate` scoped to the migrated
  database, an assert-only sync mode, plus the addendum recording what adversarial
  review and live testing changed) →
  [view @d852573](https://github.com/lincolnloop/django-absurd/blob/d852573719c117857f8bc07eca46db145b80792c/docs/specs/2026-08-17-explicit-provisioning-design.md)
- 2026-08-17 — explicit-provisioning implementation plan (the measured blast radius that
  collapsed a two-PR split into one, and the sequence that removed both self-heal seams)
  →
  [view @d852573](https://github.com/lincolnloop/django-absurd/blob/d852573719c117857f8bc07eca46db145b80792c/docs/plans/2026-08-17-explicit-provisioning.md)
