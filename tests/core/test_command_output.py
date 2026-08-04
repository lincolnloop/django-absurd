import io

import pytest
from django.core.management import call_command
from pytest_django.fixtures import SettingsWrapper

from django_absurd.test import AbsurdTestRuntime
from tests import utils

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


def test_sync_queues_decorates_its_console_output(settings: SettingsWrapper) -> None:
    settings.TASKS = utils.make_tasks_settings(queues={"freshemoji": {}})
    out = io.StringIO()
    call_command("absurd_sync_queues", stdout=out)

    assert out.getvalue() == "🗃️ Created: freshemoji\n"


def test_the_worker_banner_carries_the_elephant(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    out = io.StringIO()
    call_command("absurd_worker", queue="default", burst=True, stdout=out)

    assert out.getvalue() == (
        "🐘 Started worker on queue 'default'.\n🐘 Stopped worker on queue 'default'.\n"
    )


def test_worker_banner_keeps_the_elephant_on_a_utf8_stream(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    buffer = io.BytesIO()
    out = io.TextIOWrapper(buffer, encoding="utf-8")
    call_command("absurd_worker", queue="default", burst=True, stdout=out)
    out.flush()

    assert buffer.getvalue().decode("utf-8") == (
        "🐘 Started worker on queue 'default'.\n🐘 Stopped worker on queue 'default'.\n"
    )


def test_worker_banner_drops_the_elephant_on_a_stream_that_cannot_encode_it(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    buffer = io.BytesIO()
    out = io.TextIOWrapper(buffer, encoding="cp1252")
    call_command("absurd_worker", queue="default", burst=True, stdout=out)
    out.flush()

    assert buffer.getvalue().decode("cp1252") == (
        "Started worker on queue 'default'.\nStopped worker on queue 'default'.\n"
    )


def test_sync_queues_drops_the_crate_on_a_stream_that_cannot_encode_it(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = utils.make_tasks_settings(queues={"freshemojicp1252": {}})
    buffer = io.BytesIO()
    out = io.TextIOWrapper(buffer, encoding="cp1252")
    call_command("absurd_sync_queues", stdout=out)
    out.flush()

    assert buffer.getvalue().decode("cp1252") == "Created: freshemojicp1252\n"


def test_sync_queues_probes_the_warning_glyph_against_stderr_not_stdout(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = utils.make_tasks_settings(queues={"driftcp1252": {}})
    call_command("absurd_sync_queues")  # create 'driftcp1252' unpartitioned
    settings.TASKS = utils.make_tasks_settings(
        queues={"driftcp1252": {"storage_mode": "partitioned"}}
    )
    out_buffer = io.BytesIO()
    out = io.TextIOWrapper(out_buffer, encoding="utf-8")
    err_buffer = io.BytesIO()
    err = io.TextIOWrapper(err_buffer, encoding="cp1252")
    call_command("absurd_sync_queues", stderr=err, stdout=out)
    out.flush()
    err.flush()

    assert out_buffer.getvalue().decode("utf-8") == "🗃️ No queues to sync.\n"
    assert err_buffer.getvalue().decode("cp1252") == (
        "Queue 'driftcp1252': storage_mode cannot be changed "
        "(existing: 'unpartitioned', declared: 'partitioned'); skipping.\n"
    )


def test_sync_queues_prefixes_each_alias_when_multiple_backends_are_configured(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {
        "one": {
            "BACKEND": utils.ABSURD_BACKEND,
            "OPTIONS": {"QUEUES": {"multi-one": {}}},
        },
        "two": {
            "BACKEND": utils.ABSURD_BACKEND,
            "OPTIONS": {"QUEUES": {"multi-two": {}}},
        },
    }
    out = io.StringIO()
    call_command("absurd_sync_queues", stdout=out)

    assert out.getvalue() == (
        "🗃️ [one] Created: multi-one\n🗃️ [two] Created: multi-two\n"
    )
