# django-absurd benchmark harness image. Deps are pinned in benchmarks/pyproject.toml
# and benchmarks/uv.lock, not here, following the lincolnloop django-layout Dockerfile
# convention. Build context = repo root.
FROM python:3.14.6-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH=/opt/venv/bin:${PATH}
# No .git in the build context (.dockerignore) — feed hatch-vcs a version.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

# The django-absurd package (the harness's editable `..` path dependency) plus the
# harness's own pyproject, laid out so `[tool.uv.sources] path=".."` resolves to /src.
# `uv sync --locked` installs exactly benchmarks/uv.lock, failing the build if that
# lock has drifted from the pyproject beside it.
WORKDIR /src
COPY pyproject.toml README.md ./
COPY django_absurd ./django_absurd
COPY benchmarks/pyproject.toml benchmarks/uv.lock ./benchmarks/
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --locked --no-install-project --project /src/benchmarks

# The harness itself; the bind mount in compose.yaml overlays it live at run time.
# It stays a plain package under /src rather than an installed distribution, so
# `python -m benchmarks.stages` resolves it from the working directory.
COPY benchmarks ./benchmarks

# Bare: the driver runs every stage when given none. The migrate service overrides it.
CMD ["python", "-m", "benchmarks.stages"]
