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
