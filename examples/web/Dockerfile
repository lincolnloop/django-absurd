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
