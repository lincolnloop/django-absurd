import json

import psycopg
import psycopg.abc
from absurd_sdk import Absurd
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from psycopg.types.json import set_json_loads

BACKEND_ERROR_MESSAGE = (
    "django-absurd requires the psycopg (v3) PostgreSQL backend. "
    "See https://www.psycopg.org/psycopg3/docs/"
)


def validate_backend(using: str) -> None:
    conn = connections[using]
    conn.ensure_connection()
    if not isinstance(conn.connection, psycopg.Connection):
        raise ImproperlyConfigured(BACKEND_ERROR_MESSAGE)


def register_jsonb_loader(context: psycopg.abc.AdaptContext) -> None:
    # absurd-sdk returns jsonb columns as raw strings unless we register a loader;
    # psycopg3's built-in loader is jsonb-type-OID only and doesn't cover the
    # un-typed bytea path the SDK's claim_tasks cursor uses.
    # Accepts either a Connection (for the SDK path) or a Cursor (for get_result,
    # where cursor-scope avoids poisoning Django's shared connection adapters).
    set_json_loads(json.loads, context)


def build_absurd_client(using: str) -> Absurd:
    validate_backend(using)
    return Absurd(connections[using].connection)
