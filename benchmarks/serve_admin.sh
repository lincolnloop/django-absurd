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
from psycopg import sql

target = psycopg.conninfo.conninfo_to_dict(os.environ["DATABASE_URL"])
database = target.pop("dbname")
with psycopg.connect(**target, dbname="postgres", autocommit=True) as connection:
    held = connection.execute(
        "select 1 from pg_database where datname = %s", (database,)
    ).fetchone()
    if held is None:
        connection.execute(
            sql.SQL("create database {}").format(sql.Identifier(database))
        )
        print(f"created database {database}")
PY

uv run python manage.py migrate
uv run python -m seed --rows "$rows"

# Same story for the superuser, except that only ONE failure means "already there";
# anything else has to reach the terminal rather than be swallowed as that.
if ! superuser_output=$(DJANGO_SUPERUSER_PASSWORD=admin uv run python manage.py \
    createsuperuser --noinput --username admin --email admin@example.com 2>&1); then
    case "$superuser_output" in
    *"That username is already taken."*) ;;
    *)
        printf '%s\n' "$superuser_output" >&2
        exit 1
        ;;
    esac
fi

printf '\nlog in as admin/admin at http://localhost:%s/admin/\n\n' "$addrport"
uv run python manage.py runserver "$addrport"
