import json
import typing as t
from contextlib import contextmanager

import psycopg
import psycopg.abc
from absurd_sdk import Absurd
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from psycopg.types.json import set_json_loads

from django_absurd.hooks import build_absurd_hooks

BACKEND_ERROR_MESSAGE = (
    "django-absurd requires the psycopg (v3) PostgreSQL backend. "
    "See https://www.psycopg.org/psycopg3/docs/"
)

CRON_DATABASE_NAME_UNSET_MESSAGE = (
    "cron.database_name is not set — this PostgreSQL server has no pg_cron"
    " (add pg_cron to shared_preload_libraries and set cron.database_name)."
)


def validate_backend(using: str) -> None:
    conn = connections[using]
    conn.ensure_connection()
    if not isinstance(conn.connection, psycopg.Connection):
        raise ImproperlyConfigured(BACKEND_ERROR_MESSAGE)


def register_jsonb_loader(context: psycopg.abc.AdaptContext) -> None:
    # absurd-sdk returns jsonb columns as raw strings unless we register a loader;
    # psycopg3's built-in loader is jsonb-type-OID only and doesn't cover the
    # un-typed bytea path the SDK's claim_tasks cursor uses. Typed as an AdaptContext
    # because that is what psycopg scopes a loader to.
    set_json_loads(json.loads, context)


def build_absurd_client(using: str) -> Absurd:
    validate_backend(using)
    return Absurd(connections[using].connection, hooks=build_absurd_hooks())


def resolve_cron_database(alias: str) -> str:
    with connections[alias].cursor() as cur:
        cur.execute("select current_setting('cron.database_name', true)")
        (dbname,) = cur.fetchone()
    if dbname is None:
        raise ImproperlyConfigured(CRON_DATABASE_NAME_UNSET_MESSAGE)
    return t.cast("str", dbname)


@contextmanager
def open_central_connection(alias: str) -> t.Iterator[psycopg.Cursor[t.Any]]:
    # DEDICATED short-lived connection to the CENTRAL pg_cron metadata DB (auto-
    # discovered via resolve_cron_database), NOT Django's registered connection —
    # cron.job/cron.job_run_details live there, which may differ from the app DB.
    params: dict[str, t.Any] = connections[alias].get_connection_params()
    params.pop("cursor_factory", None)
    params["dbname"] = resolve_cron_database(alias)
    conn = psycopg.connect(**params, autocommit=True)
    try:
        with connections[alias].wrap_database_errors, conn.cursor() as cur:
            yield cur
    finally:
        conn.close()
