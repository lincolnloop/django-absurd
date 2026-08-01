# django-absurd example image. Deps come from this example's pyproject.toml and
# uv.lock (NOT pinned inline here), following the lincolnloop django-layout
# Dockerfile convention (ll:startproject). Build context = repo root.
FROM python:3.14.6-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH=/opt/venv/bin:${PATH}
# No .git in the build context (.dockerignore) — feed hatch-vcs a version.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

# The django-absurd package (this example's editable `../..` path dependency) plus
# the example's pyproject, laid out so `[tool.uv.sources] path="../.."` resolves to
# /src. `uv sync --locked` installs exactly examples/web/uv.lock, failing the
# build if that lock has drifted from the pyproject beside it.
WORKDIR /src
COPY pyproject.toml README.md ./
COPY django_absurd ./django_absurd
COPY examples/web/pyproject.toml examples/web/uv.lock ./examples/web/
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --locked --no-install-project --project /src/examples/web

# The single-file app; the bind mount in compose.yaml overlays it live at run time.
WORKDIR /app
COPY examples/web/app.py ./

EXPOSE 8000
# `nanodjango run` applies migrations + creates the admin/admin superuser
# (idempotent), then serves. The worker service overrides `command:`.
CMD ["nanodjango", "run", "app.py", "0.0.0.0:8000", "--user=admin", "--pass=admin"]
