# Build context is the REPO ROOT (not examples/) so the local django-absurd source
# is available to the example's path dependency. Build via compose — see compose.yaml.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
# Autoload the virtualenv: putting its bin first means `python manage.py ...` runs in
# the venv directly — no `uv run` prefix needed (django-layout's pattern).
ENV PATH=/repo/examples/.venv/bin:${PATH}
ENV DJANGO_SETTINGS_MODULE=demo_project.settings
# The build context has no .git (see .dockerignore), so hatch-vcs/setuptools-scm can't
# derive django-absurd's version — feed it a pretend version for the in-image build.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

WORKDIR /repo
# Only what's needed to build+install django-absurd and the example (keeps the image
# free of .venv/.tox/.git from the host).
COPY pyproject.toml README.md ./
COPY django_absurd ./django_absurd
COPY examples ./examples

WORKDIR /repo/examples
RUN uv sync --locked

# Default: run the whole demo end-to-end, then exit. (Override `command:` in compose to
# run a long-lived blocking worker instead.)
CMD ["sh", "-c", "\
  python manage.py migrate && \
  python manage.py enqueue_demo && \
  python manage.py absurd_worker --burst"]
