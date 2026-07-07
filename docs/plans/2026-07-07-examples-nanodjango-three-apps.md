# Three nanodjango examples (web / beat / pg_cron) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single dual-scheduler example with three self-contained
single-file nanodjango apps — `web` (enqueue+result), `beat` (beat scheduler + admin),
`pg_cron` (pg_cron scheduler + admin) — each in its own dir with its own isolated
compose.

**Architecture:** Per app: `app.py` (nanodjango `Django(...)` + `@task`s + optional
`@app.route` views), `compose.yaml` (own Postgres + a server on host 8000 + a
worker/±beat), `Dockerfile` (uv-sync deps from the app's `pyproject.toml`),
`pyproject.toml` (nanodjango/django/psycopg + django-absurd editable path `../..`),
`README.md`. Run one app at a time. django-absurd source bind-mounted (dev style).

**Tech Stack:** nanodjango 0.16.x, Django 6.0, psycopg3, django-absurd (editable),
Docker Compose, Postgres 18 (+ pg_cron on the pg_cron app), absurd_sdk.

## Global Constraints

- Single-file nanodjango `app.py` per app;
  `Django(ADMIN_URL="admin/", EXTRA_APPS=[...], DATABASES=..., TASKS=..., LOGGING=...)`.
- **psycopg3 required** — nanodjango defaults to sqlite, so `DATABASES` is overridden to
  `django.db.backends.postgresql`.
- **nanodjango does migrate + superuser itself** — the `app` service's Dockerfile CMD is
  `["nanodjango","run","app.py","0.0.0.0:8000","--user=admin","--pass=admin"]`, which
  makes+applies migrations and creates the **admin/admin** superuser (idempotent) before
  serving. So NO migrate one-shot, NO `start.sh`, NO `createsuperuser` step, NO
  healthchecks. `restart: on-failure` on `app`+`worker` rides out Postgres warmup and
  the worker-before-migrate race. (This is `origin/main:examples/`'s solved pattern —
  `examples/web/` @ a45b298 is the built, verified template; Tasks 2–3 mirror it.)
- Server on host **8000** (reachable); each app's compose is independent → all reuse
  8000 (run one at a time). db = `postgres:18.4-alpine`, volume
  `pgdata:/var/lib/postgresql`.
- pg_cron app: extension created by the **`django_absurd.pg_cron` app migration** (no
  demo migration); its Postgres needs `shared_preload_libraries=pg_cron` +
  `cron.database_name=<db>` (server GUCs, in compose `command`).
- These are **demo artifacts**, verified LIVE (`docker compose up`) — no unit tests.
- Bundle into `pgcron-scheduler` (draft PR #43). Commit per task.
- nanodjango run: `nanodjango run app.py 0.0.0.0:8000`; management:
  `nanodjango manage app.py <cmd>`.

---

### Task 1: `examples/web/` — enqueue + result (and remove the old example)

**Files:**

- Delete: `examples/config/`, `examples/demo/`, `examples/manage.py`,
  `examples/compose.yaml`, `examples/Dockerfile`, `examples/pyproject.toml`,
  `examples/README.md` (the old single project)
- Create: `examples/web/app.py`, `examples/web/compose.yaml`, `examples/web/Dockerfile`,
  `examples/web/pyproject.toml`, `examples/web/start.sh`, `examples/web/README.md`

**Interfaces:**

- Produces: the shared per-app patterns (`Dockerfile`, `pyproject.toml`, `start.sh`)
  that Tasks 2–3 mirror with small deltas.

- [ ] **Step 1: Remove the old example**

```bash
git rm -r examples/config examples/demo examples/manage.py examples/compose.yaml examples/Dockerfile examples/pyproject.toml examples/README.md
```

- [ ] **Step 2: `examples/web/pyproject.toml`**

```toml
[project]
name = "django-absurd-example-web"
version = "0"
requires-python = ">=3.12"
dependencies = [
  "nanodjango",
  "django-absurd",
  "django==6.0.6",
  "psycopg[binary]==3.3.4",
]

# django-absurd from the local checkout (repo root), so the demo exercises THIS
# branch's code — not a released version.
[tool.uv.sources]
django-absurd = { path = "../..", editable = true }
```

- [ ] **Step 3: `examples/web/Dockerfile`** (build context = repo root, per compose)

```dockerfile
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:${PATH}
# No .git in the build context (see .dockerignore) — feed hatch-vcs a version.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

# Copy the django-absurd package (the editable path dep) + the example's pyproject,
# preserving the `../..` relationship so `[tool.uv.sources]` resolves.
WORKDIR /src
COPY pyproject.toml README.md ./
COPY django_absurd ./django_absurd
COPY examples/web/pyproject.toml ./examples/web/pyproject.toml
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --no-install-project --project /src/examples/web

WORKDIR /app
```

(`django-absurd = {path="../.."}` from `/src/examples/web` resolves to `/src` — the
package root. Run-time bind mount overlays `/app` + `/src/django_absurd`.)

- [ ] **Step 4: `examples/web/start.sh`** (server service entrypoint: migrate →
      superuser → serve)

```bash
#!/usr/bin/env sh
set -e
nanodjango manage app.py migrate
nanodjango manage app.py createsuperuser --noinput || true   # idempotent across restarts
exec nanodjango run app.py 0.0.0.0:8000
```

(`createsuperuser --noinput` reads `DJANGO_SUPERUSER_*` env; `|| true` so a second run —
user exists — doesn't abort.)

- [ ] **Step 5: `examples/web/app.py`** (revived from the pre-`5d5051b` example,
      scheduler removed)

```python
"""Single-file nanodjango demo: django-absurd enqueue + result.

Enqueue add(a, b) from a form; the worker runs it; watch the result page and
browse the read-only queue tables in the admin (auto-registered by django-absurd).

    docker compose up
    http://localhost:8000/         enqueue add(a, b)
    http://localhost:8000/admin/   Tasks / Runs / … (superuser: admin / admin)

psycopg (v3) backend required — DATABASES is overridden (nanodjango defaults to sqlite).
"""

import dataclasses
import html
import logging
import os
import pprint

from django import forms
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.tasks import TaskResultStatus, default_task_backend, task
from django.tasks.exceptions import TaskResultDoesNotExist
from nanodjango import Django

app = Django(
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "postgres"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    },
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}},
        }
    },
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {"django_absurd": {"handlers": ["console"], "level": "INFO"}},
    },
)

logger = logging.getLogger("demo")


@task
def add(a: str, b: str) -> float:
    """Runs in the worker. Coerces here so non-numeric input FAILS the task
    (rather than being rejected up front)."""
    return float(a) + float(b)


class AddForm(forms.Form):
    a = forms.CharField(label="A")
    b = forms.CharField(label="B")


@app.route("/")
def index(request: HttpRequest) -> HttpResponse | str:
    if request.method == "POST":
        form = AddForm(request.POST)
        if form.is_valid():
            result = add.enqueue(**form.cleaned_data)
            return redirect(f"/tasks/{result.id}/")
    else:
        form = AddForm()
    return f"""
        <h1>django-absurd demo</h1>
        <p>Enqueue <code>add(a, b)</code>; the worker runs it.</p>
        <form method="post">
          <input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">
          {form.as_p()}
          <button type="submit">Add</button>
        </form>
        <p><a href="/admin/">Browse the queues in the admin</a> (admin / admin)</p>
    """


@app.route("/tasks/<str:result_id>/")
def task_detail(request: HttpRequest, result_id: str) -> HttpResponse | str:
    try:
        result = default_task_backend.get_result(result_id)
    except TaskResultDoesNotExist:
        return HttpResponse(f"<h1>Unknown task {result_id}</h1>", status=404)

    finished = result.status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED)
    refresh = "" if finished else '<meta http-equiv="refresh" content="1">'
    if result.status == TaskResultStatus.SUCCESSFUL:
        body = f"<p>Result: <strong>{result.return_value}</strong></p>"
    elif result.status == TaskResultStatus.FAILED:
        body = f"<p>Failed: {result.errors}</p>"
    else:
        body = "<p>Working… (auto-refreshing)</p>"

    fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
    dump = html.escape(pprint.pformat(fields))
    return f"""
        {refresh}
        <h1>Task {result.id}</h1>
        <p>Status: <strong>{result.status.name}</strong></p>
        {body}
        <pre><code>{dump}</code></pre>
        <p><a href="/">Add another</a> · <a href="/admin/">Admin</a></p>
    """


if __name__ == "__main__":
    app.run()
```

- [ ] **Step 6: `examples/web/compose.yaml`**

```yaml
---
services:
  db:
    image: postgres:18
    environment:
      - POSTGRES_PASSWORD=postgres
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 10
  web:
    build:
      context: ../..
      dockerfile: examples/web/Dockerfile
    command: sh start.sh
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
    environment: &env
      - PGHOST=db
      - PGPORT=5432
      - PGUSER=postgres
      - PGPASSWORD=postgres
      - PGDATABASE=postgres
      - DJANGO_SUPERUSER_USERNAME=admin
      - DJANGO_SUPERUSER_PASSWORD=admin
      - DJANGO_SUPERUSER_EMAIL=admin@example.com
    volumes: &mounts
      - ./:/app
      - ../../django_absurd:/src/django_absurd
  worker:
    build:
      context: ../..
      dockerfile: examples/web/Dockerfile
    command: nanodjango manage app.py absurd_worker
    depends_on:
      web:
        condition: service_started
    environment: *env
    volumes: *mounts
```

- [ ] **Step 7: `examples/web/README.md`** — one-screen: what it demos
      (enqueue+result+admin), `docker compose up`, open `/` + `/admin/` (admin/admin),
      note it installs django-absurd from the local checkout.

- [ ] **Step 8: Live-verify web**

```bash
cd examples/web
docker compose down -v
docker compose up --build -d
```

Then: `curl -s -X POST -d 'a=2&b=3' localhost:8000/` follows to `/tasks/<id>/`; poll
`curl -s localhost:8000/tasks/<id>/` until `Result: 5.0`; confirm `/admin/` reachable
(302→login). `docker compose logs worker` shows the task ran. Tear down
`docker compose down -v`. If Docker unavailable, say so + static-check.

- [ ] **Step 9: Commit**

```bash
git add -A examples
git commit -m "examples: web nanodjango app (enqueue+result); remove old single example"
```

---

### Task 2: `examples/beat/` — beat scheduler + admin

**Files:**

- Create:
  `examples/beat/{app.py,compose.yaml,Dockerfile,pyproject.toml,start.sh,README.md}`

**Interfaces:**

- Consumes: the Task 1 Dockerfile/pyproject/start.sh pattern (mirror, changing
  `web`→`beat` in paths + pyproject name).

- [ ] **Step 1: pyproject / Dockerfile / start.sh** — copy Task 1's, replacing
      `examples/web`→`examples/beat` and `name = "django-absurd-example-beat"`.
      `start.sh` identical.

- [ ] **Step 2: `examples/beat/app.py`**

```python
"""nanodjango demo: django-absurd BEAT scheduler.

An in-process beat fires `tick` every minute; the worker (run with --beat) drains
it and logs 'tock ⏰'. Watch Tasks/Runs fill in the admin.

    docker compose up
    http://localhost:8000/admin/   Tasks / Runs / … (superuser: admin / admin)
"""

import logging
import os

from django.tasks import task
from nanodjango import Django

app = Django(
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "postgres"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    },
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULER": "beat",  # in-process beat (the default)
                "SCHEDULE": {"tick": {"task": "app.tick", "cron": "* * * * *"}},
            },
        }
    },
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {
            "django_absurd": {"handlers": ["console"], "level": "INFO"},
            "demo": {"handlers": ["console"], "level": "INFO"},
        },
    },
)

logger = logging.getLogger("demo")


@task
def tick() -> None:
    """Fired every minute by the beat; the worker runs it and logs 'tock ⏰'."""
    logger.info("tock ⏰")


if __name__ == "__main__":
    app.run()
```

- [ ] **Step 3: `examples/beat/compose.yaml`** — same as web's, but the `worker` command
      runs the beat co-located:

```yaml
  worker:
    build:
      context: ../..
      dockerfile: examples/beat/Dockerfile
    command: nanodjango manage app.py absurd_worker --beat
    depends_on:
      web:
        condition: service_started
    environment: *env
    volumes: *mounts
```

(db + web services identical to web's compose except
`dockerfile: examples/beat/Dockerfile`.)

- [ ] **Step 4: README** — demos beat scheduling; `docker compose up`; watch `/admin/`
      Tasks/Runs grow each minute + `tock ⏰` in `docker compose logs worker`.

- [ ] **Step 5: Live-verify beat**

```bash
cd examples/beat && docker compose down -v && docker compose up --build -d
```

Within ~90s: `docker compose logs worker | grep 'tock ⏰'` non-empty; `/admin/`
reachable. Tear down. (Docker-unavailable → static-check + say so.)

- [ ] **Step 6: Commit**

```bash
git add -A examples/beat
git commit -m "examples: beat nanodjango app (beat scheduler + admin)"
```

---

### Task 3: `examples/pg_cron/` — pg_cron scheduler + admin

**Files:**

- Create:
  `examples/pg_cron/{app.py,compose.yaml,Dockerfile,pyproject.toml,start.sh,README.md}`

**Interfaces:**

- Consumes: Task 1 patterns; the pg_cron Postgres uses the repo-root
  `Dockerfile.pg_cron`.

- [ ] **Step 1: pyproject / Dockerfile / start.sh** — mirror Task 1 with
      `examples/pg_cron` paths + `name = "django-absurd-example-pg-cron"`. `start.sh`
      identical (migrate runs the pg_cron app's CreateExtension + reconcile).

- [ ] **Step 2: `examples/pg_cron/app.py`**

```python
"""nanodjango demo: django-absurd pg_cron scheduler.

Postgres fires `ping` every minute directly via pg_cron (no beat process); the
worker drains it and logs 'pong 🏓'. The `django_absurd.pg_cron` app's migration
creates the extension. Watch Tasks/Runs in the admin.

    docker compose up
    http://localhost:8000/admin/   Tasks / Runs / … (superuser: admin / admin)
"""

import logging
import os

from django.tasks import task
from nanodjango import Django

app = Django(
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd", "django_absurd.pg_cron"],  # order: pg_cron after core
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "demo"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    },
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULER": "pg_cron",  # Postgres fires it; worker has no --beat
                "SCHEDULE": {"ping": {"task": "app.ping", "cron": "* * * * *"}},
            },
        }
    },
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {
            "django_absurd": {"handlers": ["console"], "level": "INFO"},
            "demo": {"handlers": ["console"], "level": "INFO"},
        },
    },
)

logger = logging.getLogger("demo")


@task
def ping() -> None:
    """Fired every minute by pg_cron; the worker runs it and logs 'pong 🏓'."""
    logger.info("pong 🏓")


if __name__ == "__main__":
    app.run()
```

- [ ] **Step 3: `examples/pg_cron/compose.yaml`** — `db` built from the repo-root
      `Dockerfile.pg_cron` with the GUCs; `PGDATABASE=demo` must match
      `cron.database_name`:

```yaml
---
services:
  db:
    build:
      context: ../..
      dockerfile: Dockerfile.pg_cron
    command:
      - postgres
      - -c
      - shared_preload_libraries=pg_cron
      - -c
      - cron.database_name=demo
    environment:
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=postgres
      - POSTGRES_DB=demo
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d demo"]
      interval: 5s
      timeout: 5s
      retries: 10
  web:
    build:
      context: ../..
      dockerfile: examples/pg_cron/Dockerfile
    command: sh start.sh
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
    environment: &env
      - PGHOST=db
      - PGPORT=5432
      - PGUSER=postgres
      - PGPASSWORD=postgres
      - PGDATABASE=demo
      - DJANGO_SUPERUSER_USERNAME=admin
      - DJANGO_SUPERUSER_PASSWORD=admin
      - DJANGO_SUPERUSER_EMAIL=admin@example.com
    volumes: &mounts
      - ./:/app
      - ../../django_absurd:/src/django_absurd
  worker:
    build:
      context: ../..
      dockerfile: examples/pg_cron/Dockerfile
    command: nanodjango manage app.py absurd_worker
    depends_on:
      web:
        condition: service_started
    environment: *env
    volumes: *mounts
```

- [ ] **Step 4: README** — demos DB-side scheduling; note the extension is created by
      the app migration + the `shared_preload_libraries`/`cron.database_name` server
      prerequisites (already set in compose); `docker compose down -v` before re-runs.

- [ ] **Step 5: Live-verify pg_cron**

```bash
cd examples/pg_cron && docker compose down -v && docker compose up --build -d
```

Confirm: `docker compose exec db psql -U postgres -d demo -c '\dx'` shows pg_cron;
`docker compose exec db psql -U postgres -d demo -c 'select jobname from cron.job'`
shows the reconciled job; within ~90s `docker compose logs worker | grep 'pong 🏓'`
non-empty; `/admin/` reachable. Tear down. (Docker-unavailable → static-check + say so.)

- [ ] **Step 6: Commit**

```bash
git add -A examples/pg_cron
git commit -m "examples: pg_cron nanodjango app (pg_cron scheduler + admin)"
```

---

### Task 4: `examples/README.md` index + doc cross-links (sync-docs)

**Files:**

- Create: `examples/README.md` (index)
- Modify: `README.md`, `django_absurd/AGENTS.md`, `docs/web/*` — fix any "see examples/"
  pointers that referenced the old single example.

- [ ] **Step 1: `examples/README.md`** — a short index: three demos (`web/`, `beat/`,
      `pg_cron/`), one line each + "cd into one and `docker compose up`, open
      http://localhost:8000 (admin/admin)". Note run-one-at-a-time (all bind 8000).

- [ ] **Step 2: Cross-link sweep** —
      `grep -rn "examples/" README.md django_absurd/AGENTS.md docs/web` and update any
      reference to the old `examples/` layout (config/demo/single compose) to point at
      the three apps. The pg_cron `cron-jobs.md` "runnable examples" link →
      `examples/pg_cron`.

- [ ] **Step 3: Verify + commit**

```bash
grep -rn "examples/config\|examples/demo\|examples/app.py" . --include='*.md' --include='*.py' | grep -v docs/HISTORY   # stale refs → none
git add -A
git commit -m "examples: index README + fix doc cross-links to the three apps"
```

---

## Self-Review

**Spec coverage:** web (Task 1) ✓; beat (Task 2) ✓; pg_cron (Task 3) ✓; each own dir +
isolated compose + server:8000 + worker/±beat ✓; admin:admin in start.sh ✓; pg_cron
extension via app migration (no demo migration) ✓; old example removed (Task 1 Step 1)
✓; index + cross-links (Task 4) ✓; live-verify per app ✓; run-one-at-a-time / 8000 reuse
✓.

**Placeholder scan:** concrete
`app.py`/`compose.yaml`/`Dockerfile`/`pyproject.toml`/`start.sh` shown (examples are
demo artifacts — showing them is intended, not a plan failure). READMEs described in
prose (one-screen docs). No TBD/TODO.

**Consistency:** `nanodjango run app.py 0.0.0.0:8000` + `nanodjango manage app.py <cmd>`
used uniformly; `PGDATABASE` matches `cron.database_name=demo` for pg_cron; the `../..`
editable path + `/src/examples/<app>` copy relationship consistent across the 3
Dockerfiles; superuser env keys identical; task dotted paths
`app.add`/`app.tick`/`app.ping` match the schedules (`app.tick`, `app.ping`).
