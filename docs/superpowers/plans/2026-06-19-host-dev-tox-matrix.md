# Host-based dev + tox matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> This is a tooling/config change — the "tests" are command runs with expected output,
> not pytest functions. Config blocks are the deliverable; write them verbatim.

**Goal:** Move dev/test onto the host (uv-managed), keep compose for Postgres only, add
a `tox`+`tox-uv` Python×Django matrix with min-max mypy.

**Architecture:** compose runs only `db` (host port `${PGPORT:-5432}`). pytest/tox run
on the host via uv. `tox.ini` defines generational stacks (Django 5.2 floor / 6.0
ceiling) plus two mypy envs. CI replaces the in-container job with a tox matrix (+
Postgres sidecar) and a pre-commit lint job.

**Tech Stack:** uv, tox>=4.22 + tox-uv>=1.16, pytest≥9 + pytest-django, psycopg3,
compose Postgres, GitHub Actions.

## Global Constraints

- compose = Postgres only. Drop `app` service + `docker/app/Dockerfile`. `db` publishes
  `"${PGPORT:-5432}:5432"`. Container Postgres always listens on 5432.
- Dev-group dep flips `psycopg>=3.2.0` → `psycopg[binary]>=3.2.0`; PRODUCTION
  `[project] dependencies` stays plain `psycopg>=3.2.0`.
- `tox.ini` env_list EXACTLY: `py3{10,11}-django52`, `py3{12,13,14}-django60`, `latest`,
  `py310-django52-mypy`, `py314-django60-mypy`. `[testenv]` uses
  `dependency_groups=dev`, Django pinned per factor, `uv_resolution`
  `lowest-direct`(52)/`highest`(60), `pass_env=PG*`, and `mypy:`/`!mypy:` command
  conditionals (pytest both suites for test envs; `mypy .` for mypy envs).
  `[testenv:latest]` (the lock-pinned env, named for the newest stack it tracks) uses
  `uv-venv-lock-runner`.
- Dev group also needs `wheel` (the floor env's setuptools 68 doesn't vendor it,
  breaking the `--no-isolation` packaging-test build). `[tool.mypy]` must `exclude`
  `build/`, `dist/`, `.tox/` so `mypy .` ignores leftover build copies.
- Every test env runs BOTH suites: `pytest` then `pytest tests/multidb`.
- CI `.github/workflows/ci.yml`: `tox` matrix job (Postgres `services:` sidecar, PG\*
  env) via `uvx --with tox-uv tox -e <env>` + a `lint` job
  (`uvx pre-commit run --all-files`, `SKIP: mypy`). Match the repo's existing action-pin
  style (version tags like `actions/checkout@v4`).
- CLAUDE.md run-convention flips to host/tox. New `.python-version` = `3.14` — the `dev`
  env tracks the LATEST stack (Python 3.14 + Django 6.0, pinned by `uv.lock`).
- Coverage local-only (no Codecov). Mirror `~/projects/goodconf`.
- Conventions (CLAUDE.md): `import typing as t`; absolute imports. Pre-commit
  (ruff/prettier/yamllint/check-github-workflows/pretty-format-toml) must pass.

> **Run convention:** Postgres must be up first — `docker compose up -d db`. Run tox via
> `uvx --with tox-uv tox` (no global tox needed). Host pytest via `uv run pytest`.

---

## File Structure

- `compose.yaml` — Postgres-only, env-var host port.
- `docker/app/Dockerfile` — DELETE (and the now-empty `docker/app/`).
- `.pre-commit-config.yaml` — remove the `hadolint-docker` hook (no Dockerfile remains).
- `pyproject.toml` — dev-group `psycopg`→`psycopg[binary]`.
- `uv.lock` — regenerated for the dep change.
- `tox.ini` — NEW: the matrix.
- `.python-version` — NEW: `3.14` (dev tracks latest).
- `.github/workflows/ci.yml` — replace the container job with `tox` matrix + `lint`.
- `CLAUDE.md` — flip the test-run convention.

---

## Task 1: compose Postgres-only + host port + psycopg[binary]

Establishes host-based testing: compose serves only Postgres on a configurable host
port, the `app` container is gone, and `psycopg[binary]` lets host/uv build without
system libpq.

**Files:**

- Modify: `compose.yaml`, `pyproject.toml`, `.pre-commit-config.yaml`, `uv.lock`,
  `.gitignore`
- Create: `.envrc`
- Delete: `docker/app/Dockerfile`

**Interfaces:**

- Produces: a `db` service reachable at `localhost:${PGPORT:-5432}`; host `pytest` runs
  against it with no settings change (`tests/settings.py` already defaults
  `PGHOST=localhost`, `PGPORT=5432`).

- [ ] **Step 1: Rewrite `compose.yaml`**

```yaml
---
services:
  db:
    environment:
      - POSTGRES_PASSWORD=postgres
    image: postgres:16-alpine
    ports:
      - "${PGPORT:-5432}:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data/

volumes:
  pgdata:
```

- [ ] **Step 2: Delete the app container**

Run:
`git rm docker/app/Dockerfile && rmdir docker/app 2>/dev/null; rmdir docker 2>/dev/null || true`
Expected: `docker/app/Dockerfile` removed (leave `docker/` only if other files remain).

- [ ] **Step 3: Fix `.pre-commit-config.yaml` hooks that depended on the `app`
      container**

Two changes: (a) remove the `hadolint/hadolint` repo block (`- id: hadolint-docker`) —
no Dockerfile remains to lint; (b) repoint the local `mypy` hook off the container —
`entry: docker compose run -T --rm --no-deps app mypy` → `entry: uv run mypy` (keep
`language: system`). Without (b), dropping the `app` service breaks the mypy hook.

- [ ] **Step 4: Flip the dev-group psycopg to binary**

In `pyproject.toml` `[dependency-groups]` `dev`, change `"psycopg>=3.2.0"` →
`"psycopg[binary]>=3.2.0"`. Leave `[project] dependencies` `psycopg>=3.2.0` UNCHANGED.

- [ ] **Step 4b: Reserve a local Postgres port via `.envrc` (direnv)**

Create `.envrc` reserving a non-5432 host port so local dev never collides with other
projects, and activating the venv (mirrors goodconf):

```bash
export PGPORT=5433
[ -d .venv ] && source .venv/bin/activate
```

Add `.direnv/` to `.gitignore`. (`PGPORT` is the single knob — drives both the compose
host-port mapping and Django's connection.)

- [ ] **Step 5: Relock**

Run: `uv lock` Expected: `uv.lock` updates to include `psycopg-binary`. (Note: `psycopg`
is only a DEV dep — `[project] dependencies` has no `psycopg`, so nothing in prod deps
changes.)

- [ ] **Step 6: Verify host-based tests (the RED→GREEN of this task)**

Run:

```bash
docker compose up -d db        # PGPORT from .envrc (5433)
uv run pytest -q
uv run pytest tests/multidb -q
```

Expected: `db` starts and publishes the reserved port (5433); main suite `28 passed`;
multidb suite `6 passed`. (Proves host pytest reaches compose Postgres and both suites
pass off-container, on a port that won't collide with other local projects.)

- [ ] **Step 7: Commit**

```bash
git add compose.yaml docker pyproject.toml uv.lock .pre-commit-config.yaml .envrc .gitignore
git commit -m "build: compose Postgres-only with reserved host port (.envrc); psycopg[binary] for host/dev"
```

---

## Task 2: tox matrix + `.python-version`

Adds the Python×Django matrix (+ min-max mypy) so the full version sweep runs on the
host via uv.

**Files:**

- Create: `tox.ini`, `.python-version`

**Interfaces:**

- Consumes: the `db` service + `psycopg[binary]` dev group (Task 1).
- Produces: tox envs `py3{10,11}-django52`, `py3{12,13,14}-django60`, `latest`,
  `py310-django52-mypy`, `py314-django60-mypy`, each runnable via
  `uvx --with tox-uv tox -e <env>`. (The lock-pinned env is named `latest` — it tracks
  the newest Python + Django.)

- [ ] **Step 1: Confirm RED (no tox config yet)**

Run: `uvx --with tox-uv tox -e latest` Expected: FAIL — no `tox.ini` / unknown
environment `latest`.

- [ ] **Step 2: Create `tox.ini`**

```ini
[tox]
requires =
    tox>=4.22
    tox-uv>=1.16
env_list =
    py3{10,11}-django52
    py3{12,13,14}-django60
    latest
    py310-django52-mypy
    py314-django60-mypy

[testenv]
runner = uv-venv-runner
dependency_groups = dev
deps =
    django52: django>=5.2,<5.3
    django60: django>=6.0,<6.1
uv_resolution =
    django52: lowest-direct
    django60: highest
pass_env = PG*
commands =
    !mypy: pytest {posargs}
    !mypy: pytest tests/multidb {posargs}
    mypy: mypy .

[testenv:latest]
runner = uv-venv-lock-runner
commands =
    pytest {posargs}
    pytest tests/multidb {posargs}
```

(`dependency_groups = dev` refers to the PEP-735 dev group — unrelated to the env name.)

- [ ] **Step 3: Create `.python-version`**

File `.python-version` with a single line (dev env = latest stack):

```
3.14
```

- [ ] **Step 4: GREEN — latest env + floor/ceiling test envs**

Run:

```bash
docker compose up -d db
uvx --with tox-uv tox -e latest
uvx --with tox-uv tox -e py310-django52
uvx --with tox-uv tox -e py314-django60
```

Expected: each env runs the main suite (`28 passed`) then the multidb suite
(`6 passed`); all green. uv provisions Python 3.10/3.14 as needed (`latest` uses 3.14).
NOTE: the floor env's `test_packaging` build needs `wheel` (setuptools 68 doesn't vendor
it) — `wheel` must be in the dev group (added in Task 1's dep set). And `[tool.mypy]`
must `exclude` `build/`, `dist/`, `.tox/` so `mypy .` (Step 5) doesn't choke on a
leftover `build/lib/django_absurd` copy.

- [ ] **Step 5: GREEN — min-max mypy envs**

Run:

```bash
uvx --with tox-uv tox -e py310-django52-mypy
uvx --with tox-uv tox -e py314-django60-mypy
```

Expected: both run `mypy .` and report `Success: no issues found`. (`highest` resolves
`django-stubs` 6.0.x — confirmed to support Django 6.0; the floor env resolves
`django-stubs` 5.2.x via `lowest-direct`.) If real type errors surface, fix them — do
not silence.

- [ ] **Step 6: Verify the port knob**

Run:

```bash
docker compose down
PGPORT=5433 docker compose up -d db
PGPORT=5433 uvx --with tox-uv tox -e latest
docker compose down && docker compose up -d db
```

Expected: with `PGPORT=5433`, `db` publishes 5433 and the `dev` env passes both suites
against `localhost:5433` (proves one env var drives both the port mapping and Django's
connection).

- [ ] **Step 7: Commit**

```bash
git add tox.ini .python-version
git commit -m "build: tox-uv matrix (django 5.2/6.0 generational stacks + min-max mypy)"
```

---

## Task 3: CI tox matrix + lint job + CLAUDE.md

Replaces the container-based CI job (which never ran the multidb suite) with the tox
matrix over a Postgres sidecar, adds a pre-commit lint job, and flips the documented
test convention.

**Files:**

- Modify: `.github/workflows/ci.yml`, `CLAUDE.md`

**Interfaces:**

- Consumes: the tox envs (Task 2).

- [ ] **Step 1: Rewrite `.github/workflows/ci.yml`**

```yaml
---
name: CI

"on":
  pull_request:
  push:

jobs:
  tox:
    name: ${{ matrix.env }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        env:
          - py310-django52
          - py311-django52
          - py312-django60
          - py313-django60
          - py314-django60
          - latest
          - py310-django52-mypy
          - py314-django60-mypy
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready --health-interval 10s --health-timeout 5s
          --health-retries 5
    env:
      PGHOST: localhost
      PGUSER: postgres
      PGPASSWORD: postgres
      PGDATABASE: postgres
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uvx --with tox-uv tox -e "${{ matrix.env }}"

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uvx pre-commit run --all-files
        env:
          SKIP: mypy
```

- [ ] **Step 2: Flip the CLAUDE.md test convention**

In `CLAUDE.md`, replace the testing-run line(s) that say tests run in the container
(`docker compose run --rm app pytest`; "Real Postgres via compose") with:

> Tests run on the host via uv/tox. `docker compose up -d db` provides Postgres;
> `PGPORT` sets the host port (default 5432). Single-DB suite: `uv run pytest`. Multi-DB
> suite: `uv run pytest tests/multidb`. Full Python×Django matrix + mypy:
> `uvx --with tox-uv tox`.

Keep the existing psycopg (v3) requirement note.

- [ ] **Step 3: Verify lint + workflow schema**

Run: `uvx pre-commit run --all-files` Expected: PASS — `check-github-workflows`
validates the new `ci.yml`; yamllint, prettier, ruff clean; `pretty-format-toml` leaves
`pyproject.toml` unchanged. (CI itself runs on push; the tox envs it invokes were
already proven green in Task 2.)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml CLAUDE.md
git commit -m "ci: tox matrix over Postgres sidecar + pre-commit lint job; host test docs"
```

---

## Self-Review

- **Spec coverage:** compose Postgres-only + env port + drop app (T1); psycopg[binary]
  dev / plain prod (T1); hadolint hook cleanup (T1); tox generational matrix + min-max
  mypy + both-suites + pass_env (T2); `.python-version` (T2); port-knob validation (T2);
  ci.yml tox matrix + sidecar + lint job, closing the multidb-not-in-CI gap (T3);
  CLAUDE.md flip (T3); coverage local-only (no Codecov task — nothing to add). Covered.
- **Placeholder scan:** none — all config blocks are complete and verbatim; commands
  have expected output. Action pins use version tags matching the repo's existing
  `actions/checkout@v4` style.
- **Consistency:** the env_list in `tox.ini` (T2), the CI matrix list (T3), and the spec
  match exactly (`py3{10,11}-django52`, `py3{12,13,14}-django60`, `dev`,
  `py310-django52-mypy`, `py314-django60-mypy`). `PGPORT` knob consistent T1↔T2. Both
  suites (`pytest` + `pytest tests/multidb`) consistent across T2/T3.
