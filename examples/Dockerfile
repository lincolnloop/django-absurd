# Build context is the REPO ROOT (not examples/) so the local django-absurd source
# is available to install. Build via compose — see compose.yaml.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
# Autoload the venv: its bin on PATH means `nanodjango …` runs in the venv directly.
ENV PATH=/app/.venv/bin:${PATH}
# The build context has no .git (see .dockerignore), so hatch-vcs can't derive
# django-absurd's version — feed it a pretend version for the in-image build.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

# The local django-absurd package source (installed into the example venv below).
WORKDIR /src
COPY pyproject.toml README.md ./
COPY django_absurd ./django_absurd

# The single-file nanodjango app lives at /app.
WORKDIR /app
COPY examples/app.py ./
# Editable install of the local package so a bind-mounted django_absurd source (see
# compose.yaml volumes) is picked up live by the dev server's auto-reload.
RUN uv venv && uv pip install -e /src \
  "django==6.0.6" \
  "nanodjango==0.16.3" \
  "psycopg[binary]==3.3.4"

EXPOSE 8000
# Default service: `nanodjango run` makes+applies migrations, creates the admin/admin
# superuser (idempotent), then serves the dev server. The worker service overrides
# `command:` in compose.yaml.
CMD ["nanodjango", "run", "app.py", "0.0.0.0:8000", "--user=admin", "--pass=admin"]
