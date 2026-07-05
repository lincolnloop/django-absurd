# Build context is the REPO ROOT (see compose.yaml) so the local django-absurd
# source can be installed as an editable path dependency.
#
# Dependencies come from examples/pyproject.toml (django-layout style); the app
# source is BIND-MOUNTED at run time (see compose.yaml volumes), not copied into
# the image — so edits to the example or to django_absurd are picked up live.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
# The venv lives OUTSIDE /app so the bind-mounted app source (compose.yaml)
# doesn't shadow it. Its bin on PATH makes `python` resolve to the venv.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH=/opt/venv/bin:${PATH}
# The build context has no .git (see .dockerignore), so hatch-vcs can't derive
# django-absurd's version — feed it a placeholder for the in-image build.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

# The local django-absurd package source, installed as an editable dependency
# of the example project below. The bind mount in compose.yaml overlays this
# with the live checkout at run time.
WORKDIR /src
COPY pyproject.toml README.md ./
COPY django_absurd ./django_absurd

# Install the example project's dependencies into /opt/venv: django-absurd as
# an editable install of the local /src checkout, plus the pinned Django and
# psycopg3 from examples/pyproject.toml. The app source itself is not copied —
# it arrives via the bind mount in compose.yaml.
WORKDIR /app
RUN uv venv /opt/venv && uv pip install -e /src "django==6.0.6" "psycopg[binary]==3.3.4"

EXPOSE 8000
# Default service migrates then serves; the worker service overrides `command:`.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
