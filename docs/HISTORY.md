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
