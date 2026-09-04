#!/usr/bin/env bash
# Fill the bench queue's tables and serve django-absurd's admin on them.
#
# Dev-only, on a throwaway database of its own: DEBUG is on and the login is
# admin/admin. Safe to re-run — seeding replaces the rows.
#
#   ./serve_admin.sh                a million tasks on port 8000
#   ./serve_admin.sh 50000           fewer, for a quicker loop
#   ./serve_admin.sh 50000 8100      another port, when 8000 is taken
#
# Needs the `db` service already up (`docker compose up -d db`), like the suites do.
# PGPORT picks the server (5442, the suites' plain `db`) and SAMPLE_DATABASE the
# database on it.
set -euo pipefail

rows="${1:-1000000}"
addrport="${2:-8000}"
database="${SAMPLE_DATABASE:-absurd_sample}"
port="${PGPORT:-5442}"

cd "$(dirname "$0")"

export DATABASE_URL="postgres://postgres:postgres@localhost:${port}/${database}"
export DEBUG=1

# Created over the connection rather than with `docker compose exec`: in a git worktree
# that starts a SECOND compose project, which collides on the published port.
uv run python - <<'PY'
import os

import psycopg
from psycopg import errors, sql

target = psycopg.conninfo.conninfo_to_dict(os.environ["DATABASE_URL"])
database = target.pop("dbname")
with psycopg.connect(**target, dbname="postgres", autocommit=True) as connection:
    try:
        connection.execute(
            sql.SQL("create database {}").format(sql.Identifier(database))
        )
    except errors.DuplicateDatabase:
        pass
PY

uv run python manage.py migrate
uv run python -m seed --rows "$rows"

# Not `createsuperuser --noinput`: it raises on an existing username, so a re-run needs
# its error text matched to tell "already there" from a real failure.
uv run python manage.py shell --no-imports <<'PY'
from django.contrib.auth import get_user_model

User = get_user_model()
if not User.objects.filter(username="admin").exists():
    User.objects.create_superuser("admin", password="admin")
PY

printf '\nlog in as admin/admin at http://localhost:%s/admin/\n\n' "$addrport"
uv run python manage.py runserver "$addrport"
