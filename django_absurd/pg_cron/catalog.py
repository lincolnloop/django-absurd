"""The single seam every ``cron.*`` write for django-absurd schedules routes through.

Each verb opens the CENTRAL pg_cron connection (auto-discovered via
``open_central_connection``), applies the inert test gate, and schedules jobs
cross-database with ``cron.schedule_in_database`` — binding each job to the LIVE app
database and namespacing its jobname by that database + source (via ``build_jobname``).
No advisory lock: emission is post-commit and idempotent (upserts / reconcile
self-heal), so concurrent writers converge without one.
"""

import typing as t
import uuid

import psycopg
from django.core.exceptions import ValidationError
from django.db import connections

from django_absurd.connection import open_central_connection
from django_absurd.pg_cron.detection import is_pg_cron_inert


def build_jobname(database: str, source: str, name: str = "") -> str:
    """The pg_cron job name for a schedule, namespaced by target database + source.

    ``name=""`` yields the ``starts_with`` prefix for a ``(database, source)`` lane."""
    return f"_dj:{database}:{source}:{name}"


def schedule_job(
    alias: str,
    *,
    name: str,
    source: str,
    cron: str,
    command: str,
    active: bool,
) -> None:
    """(Re)schedule one job on the app database and arm it to ``active``.

    ``cron.schedule_in_database`` is an idempotent upsert, but its ``active`` argument
    only takes effect on INSERT — it does NOT re-arm an existing (e.g. disabled) job on
    upsert (verified against pg_cron 1.6). So the enabled state is applied with an
    explicit ``cron.alter_job`` on the returned jobid, which does update it; both run on
    the central connection, so this stays a single cross-database write path."""
    if is_pg_cron_inert(alias):
        return
    app_database = resolve_app_database_name(alias)
    jobname = build_jobname(app_database, source, name)
    with open_central_connection(alias) as cur:
        cur.execute(
            "select cron.schedule_in_database(%s, %s, %s, %s, NULL, %s)",
            [jobname, cron, command, app_database, active],
        )
        (jobid,) = t.cast("tuple[int]", cur.fetchone())
        cur.execute("select cron.alter_job(%s, active := %s)", [jobid, active])


def unschedule_job(alias: str, *, name: str, source: str) -> None:
    """Remove one job, tolerating an already-gone job."""
    if is_pg_cron_inert(alias):
        return
    app_database = resolve_app_database_name(alias)
    jobname = build_jobname(app_database, source, name)
    with open_central_connection(alias) as cur:
        cur.execute("select jobid from cron.job where jobname = %s", [jobname])
        unschedule_jobids(cur, [jobid for (jobid,) in cur.fetchall()])


def unschedule_jobs_for_database(alias: str, *, source: str) -> None:
    """Unschedule every job owned by one source lane on the app database
    (``_dj:<app db>:<source>:%``), scoped so tearing down one lane never touches
    another database's or another lane's jobs."""
    if is_pg_cron_inert(alias):
        return
    app_database = resolve_app_database_name(alias)
    with open_central_connection(alias) as cur:
        cur.execute(
            "select jobid from cron.job "
            "where database = %s and starts_with(jobname, %s)",
            [app_database, build_jobname(app_database, source)],
        )
        unschedule_jobids(cur, [jobid for (jobid,) in cur.fetchall()])


def prune_jobs(alias: str, *, source: str, keep_names: list[str]) -> None:
    """Unschedule owned jobs for a source lane on the app database whose name isn't in
    keep_names — i.e. jobs with no backing row (a signal-less row delete leaves the job
    orphaned; this reconverges ``cron.job`` to the rows)."""
    if is_pg_cron_inert(alias):
        return
    app_database = resolve_app_database_name(alias)
    keep = {build_jobname(app_database, source, name) for name in keep_names}
    with open_central_connection(alias) as cur:
        cur.execute(
            "select jobid, jobname from cron.job "
            "where database = %s and starts_with(jobname, %s)",
            [app_database, build_jobname(app_database, source)],
        )
        stale = [jobid for jobid, jobname in cur.fetchall() if jobname not in keep]
        unschedule_jobids(cur, stale)


def probe_cron_grammar(alias: str, *, cron: str) -> None:
    """Validate a pg_cron schedule expression by asking pg_cron itself.

    pg_cron owns its grammar (a 5-field cron or the interval form ``<n> seconds``), so
    rather than a hand-rolled matcher we schedule a throwaway job bound to the app
    database and immediately unschedule it — nothing persists. If pg_cron rejects the
    expression, surface its own error message as a ``ValidationError`` on the cron
    field.

    The central connection is autocommit and yields a RAW psycopg cursor, so there is no
    transaction to roll back (and ``wrap_database_errors`` only translates exceptions
    escaping the whole ``with`` block): the grammar error arrives here as the
    untranslated ``psycopg.Error``, which we catch and re-raise as ``ValidationError``.
    On success the throwaway job is removed explicitly."""
    if is_pg_cron_inert(alias):
        return
    app_database = resolve_app_database_name(alias)
    probe_jobname = f"_dj:__probe__:{uuid.uuid4()}"
    with open_central_connection(alias) as cur:
        try:
            cur.execute(
                "select cron.schedule_in_database(%s, %s, %s, %s, NULL, %s)",
                [probe_jobname, cron, "select 1", app_database, True],
            )
        except psycopg.Error as exc:
            raise ValidationError(str(exc).strip()) from exc
        (jobid,) = t.cast("tuple[int]", cur.fetchone())
        cur.execute("select cron.unschedule(%s)", [jobid])


def flush_database_jobs(alias: str) -> None:
    """Scoped clear of THIS app database's django-absurd state in the central catalog:
    unschedule every owned job (``database = <app db>`` AND ``_dj:`` prefix, all lanes)
    and delete its run history (``cron.job_run_details WHERE database = <app db>``).

    Scoped by the LIVE app-database name so a flush never touches another database's
    jobs or run history — NEVER a blanket ``TRUNCATE`` / unschedule of the shared
    central catalog."""
    if is_pg_cron_inert(alias):
        return
    app_database = resolve_app_database_name(alias)
    with open_central_connection(alias) as cur:
        cur.execute(
            "select jobid from cron.job "
            "where database = %s and starts_with(jobname, %s)",
            [app_database, "_dj:"],
        )
        unschedule_jobids(cur, [jobid for (jobid,) in cur.fetchall()])
        cur.execute(
            "delete from cron.job_run_details where database = %s", [app_database]
        )


def resolve_app_database_name(alias: str) -> str:
    """The LIVE name of the app database behind ``alias`` — the ``database =>`` target
    and the ``<db>`` jobname segment. The live ``settings_dict["NAME"]`` (a test run's
    mirrored name), never ``ORIGINAL_DATABASE_NAMES``."""
    return str(connections[alias].settings_dict["NAME"])


def unschedule_jobids(cur: "psycopg.Cursor[t.Any]", jobids: list[int]) -> None:
    """Unschedule each jobid, tolerating an already-removed job.

    The central connection is autocommit, so each ``cron.unschedule`` is its own
    transaction: if the ``cron.job`` row was removed out-of-band, cron.unschedule raises
    InternalError (SQLSTATE XX000) without poisoning later statements — swallow it and
    continue. We catch the RAW ``psycopg.Error`` here: ``open_central_connection``'s
    ``wrap_database_errors`` only translates exceptions escaping the whole ``with``
    block, so a ``try`` around an individual ``cur.execute`` sees the untranslated
    psycopg exception (SQLSTATE on the exception itself, ``__cause__`` is None). Matched
    on SQLSTATE (not message) for lc_messages independence.
    """
    for jobid in jobids:
        try:
            cur.execute("select cron.unschedule(%s)", [jobid])
        except psycopg.Error as exc:
            if exc.sqlstate != "XX000":
                raise
