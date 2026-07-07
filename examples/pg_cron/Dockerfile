# Build context = repo root (see compose.yaml) so the local django-absurd source installs.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PATH=/app/.venv/bin:${PATH}
# No .git in the build context (.dockerignore) — feed hatch-vcs a version.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

# django-absurd package source, editable-installed into the app venv below.
WORKDIR /src
COPY pyproject.toml README.md ./
COPY django_absurd ./django_absurd

WORKDIR /app
COPY examples/pg_cron/app.py ./
RUN uv venv && uv pip install -e /src \
  "django==6.0.6" \
  "nanodjango==0.16.3" \
  "psycopg[binary]==3.3.4"

EXPOSE 8000
# `nanodjango run` makes+applies migrations, creates the admin/admin superuser
# (idempotent), then serves. The worker service overrides `command:`.
CMD ["nanodjango", "run", "app.py", "0.0.0.0:8000", "--user=admin", "--pass=admin"]
