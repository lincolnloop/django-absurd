# django-absurd — Spec: Host-based dev + tox matrix

Date: 2026-06-19 Status: approved-for-planning

Move dev/test off the `app` Docker container onto the host (uv-managed), keep compose
for Postgres only, and add a `tox` + `tox-uv` matrix across Python/Django versions.
Mirrors the `~/projects/goodconf` tox setup, adapted for django-absurd's real Postgres
dependency and its two test suites.

## Motivation

Baking many Pythons into one image (pyenv/deadsnakes) is heavy. uv provides interpreters
natively; `tox-uv` makes a Python×Django matrix fast. The `app` container's only job was
a consistent Python env — uv.lock + uv-pinned interpreters give that with less weight.
Tests already default `PGHOST=localhost` and `--allow-hosts` already includes
`localhost`, so host-side pytest connecting to a published compose Postgres needs no
settings change.

## compose: Postgres only, env-var port

Drop the `app` service and `docker/app/Dockerfile`. `db` publishes a host port driven by
an env var (default 5432) so agents/users avoid collisions:

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      - POSTGRES_PASSWORD=postgres
    ports:
      - "${PGPORT:-5432}:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data/
volumes:
  pgdata:
```

One knob: `PGPORT=5433 docker compose up -d db` remaps the host port AND (since
`tests/settings.py` reads `PGPORT`, default `5432`) tells Django to connect there.
Inside the container Postgres always listens on 5432.

## Dependencies

Dev deps already live in `[dependency-groups] dev` (PEP 735), so `tox-uv`'s
`dependency_groups = dev` consumes them directly. The one change: flip the dev-group
`psycopg>=3.2.0` → `psycopg[binary]>=3.2.0` (bundles libpq → host + every tox env build
with NO system libpq). The PRODUCTION dependency (`[project] dependencies`) stays plain
`psycopg>=3.2.0` (prod uses system libpq — psycopg's recommended prod form). `absurdctl`
stays a dev dep.

## tox.ini

Generational stacks (each Python tested once), mirroring goodconf:

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

- `django52` line: Django 5.2 (the floor) on the Pythons it supports, resolving deps
  `lowest-direct` to exercise the declared floors. `django60` line: latest, `highest`.
- Test envs run BOTH suites — the main suite then the nested `tests/multidb` suite
  (auto-discovers `tests/multidb/pytest.toml`; `pythonpath=["../.."]` resolves to repo
  root, same mechanism on host as in the container).
- **Min-max mypy via a `mypy` factor:** `py310-django52-mypy` type-checks the floor
  (Python 3.10 + Django 5.2 + `lowest-direct` `django-stubs`), `py314-django60-mypy` the
  ceiling (Python 3.14 + Django 6.0 + `highest` `django-stubs`). The `mypy:`/`!mypy:`
  command conditionals make the same `[testenv]` run `mypy .` for mypy envs and the two
  pytest suites otherwise — `django-stubs` (in the dev group) resolves to the version
  matching each Django line. (Risk: if no published `django-stubs` yet supports Django
  6.0, `py314-django60-mypy` surfaces it — pin/adjust at that point.)
- `pass_env = PG*` forwards `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` so each
  env reaches the compose Postgres.
- Postgres must be up first: `docker compose up -d db`.

## CI (`.github/workflows/ci.yml`)

There is ONE workflow today; its `test` job builds the `app` image and runs
`pytest tests/` in-container — note it does NOT run the `tests/multidb` suite, so
multi-DB is currently UNTESTED in CI. Replace that job with two jobs:

- **`tox`** — matrix over the env list, one job per env via
  `uvx --with tox-uv tox -e <env>`, with Postgres as a GitHub `services:` sidecar
  (cleaner than booting compose in CI). Each env runs both suites (tox `commands`), so
  multi-DB is now covered.
- **`lint`** — `uvx pre-commit run --all-files` (ruff + prettier + yamllint). mypy runs
  in the tox mypy envs; skip it in the pre-commit job (`SKIP: mypy`) to avoid a
  duplicate run.

```yaml
jobs:
  tox:
    name: ${{ matrix.env }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        env:
          [
            py310-django52,
            py311-django52,
            py312-django60,
            py313-django60,
            py314-django60,
            latest,
            py310-django52-mypy,
            py314-django60-mypy,
          ]
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_PASSWORD: postgres
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 10s --health-timeout 5s
          --health-retries 5
    env:
      PGHOST: localhost
      PGUSER: postgres
      PGPASSWORD: postgres
      PGDATABASE: postgres
    steps:
      - uses: actions/checkout@<pin to SHA at impl>
      - uses: astral-sh/setup-uv@<pin to SHA at impl>
      - run: uvx --with tox-uv tox -e "${{ matrix.env }}"
```

mypy envs need no Postgres but the sidecar is harmless. Coverage stays LOCAL-only (no
Codecov) for now. Renovate workflows (if any) unchanged. (Action SHA pins resolved
during implementation per the repo's pinning convention.)

## Conventions / docs

- CLAUDE.md: flip "tests run in the container (`docker compose run --rm app pytest`)" →
  "tests run on the host via uv/tox; `docker compose up -d db` provides Postgres;
  `PGPORT` customizes the host port (default 5432). Full matrix: `tox`." Keep the
  psycopg3 requirement note.
- `.python-version` (new file) → `3.14`: the `dev`/lock env tracks the LATEST stack
  (newest Python + Django 6.0, pinned by `uv.lock`).
- No Makefile or README exist today — none to update. (A README dev-setup section could
  be added later; out of scope here.)

## Testing / validation

- `docker compose up -d db` then `tox -e latest` → both suites green.
- `tox -e py310-django52` and `tox -e py314-django60` → both suites green (floor +
  ceiling prove the matrix end-to-end, incl. psycopg[binary] build under uv).
- `tox -e py310-django52-mypy` and `tox -e py314-django60-mypy` → both clean (floor +
  ceiling type checks).
- `uvx pre-commit run --all-files` → ruff/mypy/prettier clean.
- `PGPORT=5433 docker compose up -d db` + `PGPORT=5433 tox -e latest` → green (proves
  the port knob).

## Out of scope

Changing the production `psycopg` form; Codecov upload; the PyPI publish workflow;
adding Pythons/Django versions beyond the goodconf generational split (widen later);
removing compose entirely (Postgres still via compose).
