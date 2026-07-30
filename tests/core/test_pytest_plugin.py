import importlib
import inspect
import pathlib
import typing as t

import pytest
from django.core.management import call_command
from django.db import connections
from django.test import TransactionTestCase

if t.TYPE_CHECKING:
    import pytest_django.fixtures

from django_absurd import absurd_params, pytest_plugin
from django_absurd.flush import flush_absurd_state
from django_absurd.models import Queue, Task
from django_absurd.test import AbsurdTestRuntime, install_absurd_cleanup
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)

# The leak-recovery tests below need a real session boundary — a session-scoped fixture
# cannot be re-triggered inside the session that already ran it — so they drive a nested
# pytest run through ``pytester``.
pytest_plugins = ["pytester"]

# Each inner run needs PYTHONPATH, so the inner interpreter can import
# ``tests.core.settings``. Each inner run also needs an explicit settings module that
# pins ``DATABASES["default"]["TEST"]["NAME"]`` to THIS run's own test database:
# pytest-django takes its per-worker suffix from xdist's workerinput, which a serial
# inner run has none of, so it would otherwise compute the unsuffixed name and touch a
# database this test never planted a GUC on.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# A database that does not exist, for the inner run that has no Absurd backend: any
# statement the plugin tried to issue there would fail to connect and error the session.
ABSENT_DATABASE = "absurd_no_such_database"

# django-stubs doesn't model TransactionTestCase's internal ``_post_teardown`` hook;
# access it (and probe instances) through this alias to keep the mechanism assertions
# honest without a stub-gap ``type: ignore`` on every line.
TxnCase: t.Any = TransactionTestCase


def test_flush_absurd_state_truncates_rows_by_default() -> None:
    tasks.add.enqueue(1, 2)
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1

    flush_absurd_state()

    assert task_model.objects.filter(queue="default").count() == 0
    assert Queue.objects.filter(queue_name="default").exists()  # schema untouched


def test_flush_absurd_state_truncates_a_partitioned_queues_idempotency_table(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # `i_<queue>` only exists for a `partitioned` queue — exercise that branch of
    # truncate_queue_tables (the unpartitioned "default" queue used elsewhere in this
    # file never creates it).
    settings.TASKS = utils.make_tasks_settings(
        queues={"part": {"storage_mode": "partitioned"}}
    )
    call_command("absurd_sync_queues")
    absurd_params(idempotency_key="k").bind(tasks.add.using(queue_name="part")).enqueue(
        1, 2
    )
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="part").count() == 1

    flush_absurd_state()

    assert task_model.objects.filter(queue="part").count() == 0
    assert Queue.objects.filter(queue_name="part").exists()  # schema untouched


def test_flush_absurd_state_drops_schema_when_requested() -> None:
    tasks.add.enqueue(1, 2)

    flush_absurd_state(drop_schema=True)

    assert not Queue.objects.filter(queue_name="default").exists()


def test_flush_absurd_state_is_a_noop_on_an_unmigrated_schema(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # Mirrors tests/core/test_checks.py::test_db_unreachable_is_silent's real
    # unreachable-DB technique: mutating settings.DATABASES alone is not enough — the
    # existing psycopg connection stays open and gets reused. del connections["default"]
    # forces the NEXT use to actually attempt (and fail) a fresh connect against the
    # bogus name, which is what makes OperationalError/ProgrammingError reachable here.
    real_name = settings.DATABASES["default"]["NAME"]
    settings.DATABASES["default"]["NAME"] = "absurd_nope_missing_db"
    del connections["default"]
    try:
        flush_absurd_state()  # must not raise
    finally:
        settings.DATABASES["default"]["NAME"] = real_name
        connections["default"].close()


def test_flush_absurd_state_resets_a_stranded_fake_now() -> None:
    utils.set_database_fake_now("2036-01-01T00:00:00+00:00")
    try:
        flush_absurd_state()

        assert utils.read_database_fake_now() is None
    finally:
        utils.reset_database_fake_now()


def test_post_teardown_hook_truncates_absurd_state() -> None:
    tasks.add.enqueue(1, 2)
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1

    class ProbeCase(TransactionTestCase):
        databases = {"default"}

    probe: t.Any = ProbeCase()
    probe._post_teardown()

    assert task_model.objects.filter(queue="default").count() == 0
    assert Queue.objects.filter(queue_name="default").exists()  # truncate, not drop


def test_post_teardown_hook_skips_undeclared_absurd_alias() -> None:
    tasks.add.enqueue(1, 2)

    class NoDatabasesCase(TransactionTestCase):
        databases = set[str]()

    probe: t.Any = NoDatabasesCase()
    probe._post_teardown()

    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1  # guard skipped it


def test_post_teardown_hook_skips_without_an_absurd_backend(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # Seed under the real Absurd backend, then swap TASKS to a non-Absurd backend so
    # the hook's unconfigured-backend guard (no AbsurdBackend) fires and skips flushing.
    call_command("absurd_sync_queues")
    tasks.add.enqueue(1, 2)
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1

    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }

    class ProbeCase(TransactionTestCase):
        databases = {"default"}

    probe: t.Any = ProbeCase()
    probe._post_teardown()

    assert task_model.objects.filter(queue="default").count() == 1  # guard skipped it


def test_install_absurd_cleanup_is_idempotent() -> None:
    already_installed = TxnCase._post_teardown

    install_absurd_cleanup()

    assert TxnCase._post_teardown is already_installed


def test_install_absurd_cleanup_wraps_a_fresh_post_teardown() -> None:
    installed = TxnCase._post_teardown
    original = installed.__wrapped__
    TxnCase._post_teardown = original
    try:
        install_absurd_cleanup()

        assert TxnCase._post_teardown is not original
        assert TxnCase._post_teardown.__wrapped__ is original
    finally:
        TxnCase._post_teardown = installed


def test_the_absurd_fixture_drains_an_enqueued_task(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = tasks.add.enqueue(3, 4)
    dj_absurd.drain()
    snap = dj_absurd.get_result(result.id)
    assert snap is not None
    assert snap.state == "completed"


@pytest.mark.parametrize(
    "fixture_name", ["_sweep_orphaned_pg_cron_jobs", "_sweep_stranded_fake_now"]
)
def test_start_sweeps_declare_only_request_so_the_guard_runs_first(
    fixture_name: str,
) -> None:
    # Import-safety invariant: a session start-sweep must take ONLY ``request``.
    # Declaring ``django_db_setup``/``django_db_blocker`` as parameters would make
    # pytest resolve them BEFORE the body's ``settings.configured`` /
    # ``apps.is_installed`` guard, which in a non-Django or pytest-django-less project
    # skips or errors the whole session. The DB fixtures are pulled lazily via
    # ``getfixturevalue`` only after the guard passes.
    # django-stubs/pytest type the fixture as FixtureFunctionDefinition, which doesn't
    # model the ``__wrapped__`` original-function handle; reach it through t.Any.
    fixture: t.Any = getattr(pytest_plugin, fixture_name)
    assert list(inspect.signature(fixture.__wrapped__).parameters) == ["request"]


def test_a_stranded_fake_now_is_swept_before_the_session_runs(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """A SIGKILLed run leaves the GUC behind; the next session must clear it."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    utils.set_database_fake_now("2036-01-01T00:00:00+00:00")
    try:
        real_name = connections["default"].settings_dict["NAME"]
        pytester.makepyfile(
            inner_settings=f"""
            from tests.core.settings import *  # noqa: F403

            DATABASES["default"]["TEST"]["NAME"] = {real_name!r}
            """,
            inner_test="""
            import datetime as dt

            import pytest

            pytestmark = pytest.mark.django_db(transaction=True)


            def test_clock_is_real(dj_absurd):
                assert dj_absurd.now.year == dt.datetime.now(dt.UTC).year
            """,
        )

        outcome = pytester.runpytest_subprocess("--reuse-db", "--ds=inner_settings")

        outcome.assert_outcomes(passed=1)
        assert utils.read_database_fake_now() is None
    finally:
        utils.reset_database_fake_now()


def test_the_stranded_fake_now_sweep_skips_when_nothing_is_provisioned(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """A session with no ``django_db``-marked test anywhere provisions nothing for the
    Absurd alias — ``NAME`` stays whatever the developer configured, never swapped to
    a test database. Pointing that LIVE ``NAME`` at a database that does not exist
    proves the sweep never issues its ``ALTER DATABASE``: were the skip to regress,
    connecting to that nonexistent name would error the whole session before this
    test's own body ever ran.
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    pytester.makepyfile(
        inner_settings=f"""
        from tests.core.settings import *  # noqa: F403

        DATABASES["default"]["NAME"] = {ABSENT_DATABASE!r}
        """,
        inner_test="""
        def test_no_db_backed_test_anywhere():
            assert 1 + 1 == 2
        """,
    )

    outcome = pytester.runpytest_subprocess("--ds=inner_settings")

    outcome.assert_outcomes(passed=1)


def test_the_orphaned_pg_cron_jobs_sweep_skips_when_nothing_is_provisioned(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """Mirrors the ``fake_now`` skip test above for ``_sweep_orphaned_pg_cron_jobs``'s
    own unprovisioned-session guard.

    That fixture only proceeds past its first guard when the pg_cron app is
    INSTALLED, so the inner run needs ``tests.pg_cron.settings`` rather than this
    suite's own — no new pytester harness, just tests/core's existing machinery
    pointed at a different settings module (``tests/pg_cron`` has no pytester harness
    of its own to mirror this into). A session with no ``django_db``-marked test
    anywhere provisions nothing for the Absurd alias, so ``NAME`` stays whatever the
    developer configured. Pointing that LIVE ``NAME`` at a database that does not
    exist proves the sweep never issues its central-catalog ``DELETE``/unschedule:
    were the skip to regress, connecting to that nonexistent name would error the
    whole session before this test's own body ever ran.
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    pytester.makepyfile(
        inner_settings=f"""
        from tests.pg_cron.settings import *  # noqa: F403

        DATABASES["default"]["NAME"] = {ABSENT_DATABASE!r}
        """,
        inner_test="""
        def test_no_db_backed_test_anywhere():
            assert 1 + 1 == 2
        """,
    )

    outcome = pytester.runpytest_subprocess("--ds=inner_settings")

    outcome.assert_outcomes(passed=1)


def test_a_test_that_raises_inside_a_freeze_still_releases_the_clock(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """Proves recovery by SOME layer — the ``freeze_time`` block's own exit, the fixture
    teardown behind it, or the flush reset. All three exist by now and all three fire
    after the inner failing test; the sweep test above is the one that isolates the
    sweep, since it runs before any inner test."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    real_name = connections["default"].settings_dict["NAME"]
    pytester.makepyfile(
        inner_settings=f"""
        from tests.core.settings import *  # noqa: F403

        DATABASES["default"]["TEST"]["NAME"] = {real_name!r}
        """,
        inner_test="""
        import datetime as dt

        import pytest

        pytestmark = pytest.mark.django_db(transaction=True)


        def test_shifts_then_fails(dj_absurd):
            with dj_absurd.freeze_time() as frozen_time:
                frozen_time.shift(dt.timedelta(days=7))
                pytest.fail("deliberate")
        """,
    )
    # The inner run plants a real-now+7d GUC on the database the OUTER session shares.
    # Should every recovery layer regress at once, the reset below keeps the cost at one
    # red test: a live fake_now over a real Python clock is the deadlock direction, and
    # the next draining test in this session would hang unkillably instead of failing.
    try:
        outcome = pytester.runpytest_subprocess("--reuse-db", "--ds=inner_settings")

        outcome.assert_outcomes(failed=1)
        assert utils.read_database_fake_now() is None
    finally:
        utils.reset_database_fake_now()


def test_freeze_time_refuses_a_test_with_no_db_marker(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """A test that never earned Django DB access must never reach ANY database through
    ``freeze_time()``'s own raw connection — proven by pointing the LIVE ``NAME`` at a
    database that does not exist. A second, marked test keeps ``django_db_setup`` and
    the session sweep themselves working normally (their own aliases come from every
    marked test in the whole session, not just this one), isolating the assertion to
    the unmarked test's own guard: were it to regress, the raw connection would fail
    trying to reach that nonexistent name, not raise this curated error.
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    real_name = connections["default"].settings_dict["NAME"]
    pytester.makepyfile(
        inner_settings=f"""
        from tests.core.settings import *  # noqa: F403

        DATABASES["default"]["NAME"] = {ABSENT_DATABASE!r}
        DATABASES["default"]["TEST"]["NAME"] = {real_name!r}
        """,
        inner_test="""
        import pytest


        @pytest.mark.django_db(transaction=True)
        def test_seed_the_alias():
            pass


        def test_no_marker(dj_absurd):
            with pytest.raises(
                RuntimeError,
                match=(
                    r"django-absurd: freeze_time\\(\\) needs real Django database "
                    r"access to pin Postgres's clock on 'default', and this test "
                    r"has none\\. Mark it "
                    r"@pytest\\.mark\\.django_db\\(transaction=True\\)\\."
                ),
            ):
                with dj_absurd.freeze_time():
                    pass
        """,
    )

    outcome = pytester.runpytest_subprocess("--reuse-db", "--ds=inner_settings")

    outcome.assert_outcomes(passed=2)


def test_the_start_sweep_no_ops_in_a_project_without_an_absurd_backend(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """The bail-out arm, for real: django-absurd installed, no Absurd backend.

    The inner project points at a database that does not exist, so the promise — a
    plugin that touches nothing when no Absurd backend is configured — is observable: a
    clean session proves the sweep never resolved a database, let alone connected to
    one. Past the guard it falls back to the ``default`` alias and tries, and failing to
    connect errors every test in the session.
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    pytester.makepyfile(
        inner_settings=f"""
        from tests.core.settings import *  # noqa: F403

        TASKS = {{"default": {{"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}}}
        DATABASES["default"]["NAME"] = {ABSENT_DATABASE!r}
        """,
        inner_test="""
        from django_absurd.backends import get_absurd_backends


        def test_no_absurd_backend_is_configured():
            assert get_absurd_backends() == {}
        """,
    )

    outcome = pytester.runpytest_subprocess("--reuse-db", "--ds=inner_settings")

    outcome.assert_outcomes(passed=1)


def test_a_pytest_run_with_no_django_settings_still_collects(
    monkeypatch: pytest.MonkeyPatch, pytester: pytest.Pytester
) -> None:
    """The ``pytest11`` entry point loads on EVERY pytest run in any venv that has
    django-absurd installed — Django project or not — and ``pytest_configure`` imports
    ``django_absurd.test`` there. A module-level import in that module that reaches
    ``django_absurd.models`` defines model classes against unconfigured settings, so the
    run dies with ``INTERNALERROR: ImproperlyConfigured`` before collecting anything.

    Only a subprocess with ``DJANGO_SETTINGS_MODULE`` removed can catch that: reloading
    the plugin inside this already-configured session sees settings either way.
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE")
    pytester.makepyfile(
        inner_test="""
        def test_django_is_never_configured():
            assert 1 + 1 == 2
        """
    )

    outcome = pytester.runpytest_subprocess()

    outcome.assert_outcomes(passed=1)


def test_plugin_module_imports_cleanly() -> None:
    # The pytest11 plugin module loads during pytest's bootstrap, before pytest-cov
    # starts, so its module-level lines escape the coverage session. Reloading
    # re-executes them under coverage — and doubles as a smoke test that the shipped
    # entry-point module imports cleanly and exposes its public surface.
    importlib.reload(pytest_plugin)
    assert callable(pytest_plugin.pytest_configure)
    assert callable(pytest_plugin.dj_absurd)
