import typing as t
from html import unescape

import pytest
from bs4 import BeautifulSoup, Tag
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse, reverse_lazy

if t.TYPE_CHECKING:
    import pytest_django.fixtures
    from bs4.element import ResultSet

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons
from tests.utils import HasContent

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_changelist")
ADD = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_add")

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "QUEUES": {"default": {}, "other": {}, "reports": {}},
            "SCHEDULE": {
                "nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
                "hourly": {
                    "task": "tests.tasks.on_reports",
                    "cron": "0 * * * *",
                    "queue": "reports",
                },
            },
        },
    }
}

CHANGE_PAYLOAD = {
    "task": "tests.tasks.add",
    "queue": "default",
    "cron": "0 3 * * *",
    "enabled": "on",
    "args": "[]",
    "kwargs": "{}",
    "max_attempts": "",
    "retry_kind": "",
    "retry_base_seconds": "",
    "retry_factor": "",
    "retry_max_seconds": "",
    "headers": "",
    "cancellation_max_duration": "",
    "cancellation_max_delay": "",
    "idempotency_key": "",
}


def seed(settings: "pytest_django.fixtures.SettingsWrapper") -> None:
    settings.TASKS = TASKS
    call_command("absurd_sync_queues")
    sync_crons(get_absurd_backends()["default"])


def rows(response: HasContent) -> "ResultSet[Tag]":
    soup = BeautifulSoup(response.content, "html.parser")
    return soup.select("#result_list tbody tr")


def get_change_url(pk: int) -> str:
    return reverse("admin:django_absurd_pg_cron_scheduledtask_change", args=[pk])


def test_changelist_renders_one_row_per_schedule(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    assert len(rows(client.get(CHANGELIST))) == 2


def test_changelist_shows_expected_columns(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    body = client.get(CHANGELIST).content.decode()
    assert "nightly" in body
    assert "0 2 * * *" in body
    assert "tests.tasks.add" in body


def test_queue_filter_renders_and_narrows(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(CHANGELIST).content, "html.parser")
    assert soup.select_one('#changelist-filter a[href*="reports"]') is not None
    narrowed = rows(client.get(CHANGELIST, {"queue__exact": "reports"}))
    assert len(narrowed) == 1
    assert "hourly" in narrowed[0].get_text()


def test_search_by_name_narrows(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    assert len(rows(client.get(CHANGELIST, {"q": "nightly"}))) == 1


def test_create_resolves_all_spawn_options_and_is_disabled(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "name": "fromdecorators",
            "task": "tests.tasks.fully_specced",
            "cron": "0 2 * * *",
        },
    )
    assert response.status_code == 302
    row = ScheduledTask.objects.get(name="fromdecorators")
    assert response["Location"] == get_change_url(row.pk)
    assert row.source == "a"
    assert row.enabled is False
    assert row.queue == "reports"
    assert row.max_attempts == 9
    assert row.retry_kind == "fixed"
    assert row.retry_base_seconds == 5
    assert row.cancellation_max_duration == 45
    assert row.cancellation_max_delay == 3


def test_create_with_save_and_add_another_returns_to_the_add_view(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # "Save and add another" must NOT force the review-on-change redirect — it lands
    # back on the add view like Django's default.
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "name": "another",
            "task": "tests.tasks.add",
            "cron": "0 2 * * *",
            "_addanother": "1",
        },
    )
    assert response.status_code == 302
    assert response["Location"] == str(ADD)


def test_add_view_renders_only_the_minimal_fields(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    names = {i.get("name") for i in soup.select("#scheduledtask_form [name]")}
    assert {"name", "task", "cron"} <= names
    assert "max_attempts" not in names
    assert "retry_kind" not in names
    assert "queue" not in names


def narrow_to_default_queue_only(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    """Re-declare the backend with only "default" queue.

    Leaves tests.tasks (and its "other"/"reports"-queued tasks) already imported — so a
    create POST resolves a task against a backend that no longer declares that task's
    queue.
    """
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}},
        }
    }
    call_command("absurd_sync_queues")


def test_create_with_undeclared_resolved_queue_is_form_error_not_created(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # on_reports' own queue ("reports") resolves fine, but the backend no longer
    # declares it, so the model rejects the resolved queue. queue isn't a field on the
    # 4-field create form, so the form must re-home that queue-keyed error onto the form
    # (a non-field error) rather than raise "has no field named queue" (HTTP 500) — and
    # create nothing.
    seed(settings)  # imports tests.tasks while "reports" is declared
    narrow_to_default_queue_only(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "name": "undeclaredq",
            "task": "tests.tasks.on_reports",
            "cron": "0 2 * * *",
        },
    )
    assert response.status_code == 200
    assert "queue 'reports' is not declared." in unescape(response.content.decode())
    assert not ScheduledTask.objects.filter(name="undeclaredq").exists()


def test_create_with_unimportable_task_is_form_error_not_created(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # An unimportable task path is rejected on the task field before any spawn-option
    # resolution runs (so build_scheduled_fields never sees a bad path), and nothing is
    # created.
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "name": "bogus",
            "task": "tests.tasks.does_not_exist",
            "cron": "0 2 * * *",
        },
    )
    assert response.status_code == 200
    assert "task 'tests.tasks.does_not_exist' could not be imported" in unescape(
        response.content.decode()
    )
    assert not ScheduledTask.objects.filter(name="bogus").exists()


def test_create_with_non_task_target_is_form_error_not_created(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # A path that imports but isn't a Django task is rejected on the task field (so
    # resolution, which reads task-only attributes, is never reached).
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "name": "notatask",
            "task": "os.getpid",
            "cron": "0 2 * * *",
        },
    )
    assert response.status_code == 200
    assert "'os.getpid' is not a Django task." in unescape(response.content.decode())
    assert not ScheduledTask.objects.filter(name="notatask").exists()


def test_create_with_blank_task_skips_resolution_and_is_required_error(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # A blank task never reaches cleaned_data, so spawn-option resolution is skipped
    # entirely; the form reports the field as required and creates nothing.
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {"name": "notask", "task": "", "cron": "0 2 * * *"},
    )
    assert response.status_code == 200
    assert "This field is required." in response.content.decode()
    assert not ScheduledTask.objects.filter(name="notask").exists()


def test_create_with_bad_cron_is_form_error_not_created(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "name": "badcron",
            "task": "tests.tasks.add",
            "cron": "not a cron",
        },
    )
    assert response.status_code == 200
    assert not ScheduledTask.objects.filter(name="badcron").exists()


def test_create_and_sync_produce_identical_spawn_columns(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # parity: admin-creating a task resolves the same spawn columns as running
    # sync_crons over a settings SCHEDULE of that same task (the two write lanes).
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULE": {
                    "settings_specced": {
                        "task": "tests.tasks.fully_specced",
                        "cron": "0 2 * * *",
                    }
                },
            },
        }
    }
    call_command("absurd_sync_queues")
    sync_crons(get_absurd_backends()["default"])
    client.force_login(admin_user)
    client.post(
        ADD,
        {
            "name": "admin_specced",
            "task": "tests.tasks.fully_specced",
            "cron": "0 2 * * *",
        },
    )
    admin_row = ScheduledTask.objects.get(source="a", name="admin_specced")
    settings_row = ScheduledTask.objects.get(source="s", name="settings_specced")
    cols = (
        "cancellation_max_delay",
        "cancellation_max_duration",
        "headers",
        "idempotency_key",
        "max_attempts",
        "queue",
        "retry_base_seconds",
        "retry_factor",
        "retry_kind",
        "retry_max_seconds",
    )
    for col in cols:
        assert getattr(admin_row, col) == getattr(settings_row, col)


def test_add_link_present_and_add_view_renders(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    changelist = client.get(CHANGELIST)
    assert changelist.status_code == 200
    soup = BeautifulSoup(changelist.content, "html.parser")
    assert soup.select_one(".object-tools a.addlink") is not None
    add = client.get(ADD)
    assert add.status_code == 200


def test_add_view_cron_help_renders_pg_cron_link_as_html(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # the cron field's help text embeds an <a> to the pg_cron docs; Django form
    # help_text is not auto-escaped, so it must reach the page as a real anchor (an
    # escaped &lt;a&gt; would be inert text, not a clickable link). BeautifulSoup
    # finding it as an <a> element inside the cron row proves it rendered as HTML.
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    link = soup.select_one(
        '.field-cron .help a[href="https://github.com/citusdata/pg_cron"]'
    )
    assert link is not None
    assert link.get_text() == "pg_cron"


def create_scheduled_task(
    client: Client,
    name: str,
    task: str = "tests.tasks.add",
    cron: str = "0 3 * * *",
) -> int:
    """Create source="admin" row through minimal create form; return its pk."""
    client.post(ADD, {"name": name, "task": task, "cron": cron})
    return ScheduledTask.objects.get(name=name).pk


def test_change_view_shows_resolved_default_max_attempts(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # The create form resolves max_attempts from the task/backend (here the backend
    # default, 5); the change form then renders that resolved value editable.
    seed(settings)
    client.force_login(admin_user)
    pk = create_scheduled_task(client, name="maxattempts")
    soup = BeautifulSoup(client.get(get_change_url(pk)).content, "html.parser")
    field = soup.select_one('input[name="max_attempts"]')
    assert field is not None
    assert field.get("value") == "5"


def test_posting_add_creates_admin_schedule_and_schedules_job(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {**CHANGE_PAYLOAD, "name": "fromadmin"})
    assert response.status_code == 302
    assert ScheduledTask.objects.get(name="fromadmin").source == "a"
    assert ScheduledTask.pg_cron.get_job("fromadmin", "a") is not None


def test_posting_add_with_tampered_source_is_forced_to_admin(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # source is a hidden, pinned field: a crafted POST setting source="s" (settings)
    # must not create a settings-owned row via the writable admin.
    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {**CHANGE_PAYLOAD, "name": "tamper", "source": "s"})
    assert response.status_code == 302
    assert ScheduledTask.objects.get(name="tamper").source == "a"


def test_editing_over_long_name_row_is_form_error_not_500(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # A row whose name overflows the 63-byte jobname budget (created out-of-band, so it
    # skipped full_clean) must surface a form error on edit, not HTTP 500 — clean()
    # keys the jobname-length error to NON_FIELD_ERRORS, not the read-only "name" field.
    seed(settings)
    client.force_login(admin_user)
    row = ScheduledTask.objects.create(
        source="a",
        name="x" * 60,
        task="tests.tasks.add",
        cron="0 3 * * *",
    )
    response = client.post(
        get_change_url(row.pk), {**CHANGE_PAYLOAD, "cron": "0 4 * * *"}
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert "job name exceeds 63 bytes (composed name" in content
    assert "Postgres silently truncates longer names)." in content


def test_editing_blank_args_kwargs_falls_back_to_defaults(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    # queue="reports" (declared) so the edit exercises the "stored queue is still a
    # valid choice" path (no injection) alongside the blank args/kwargs fallback.
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**CHANGE_PAYLOAD, "name": "editblank", "queue": "reports"})
    row = ScheduledTask.objects.get(name="editblank")
    response = client.post(
        get_change_url(row.pk),
        {**CHANGE_PAYLOAD, "queue": "reports", "args": "", "kwargs": ""},
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.args == []
    assert row.kwargs == {}


def test_posting_duplicate_admin_name_is_form_error_not_500(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**CHANGE_PAYLOAD, "name": "dup"})
    response = client.post(ADD, {**CHANGE_PAYLOAD, "name": "dup"})
    assert response.status_code == 200  # re-rendered with a form error, not HTTP 500
    assert ScheduledTask.objects.filter(source="a", name="dup").count() == 1
    assert (
        "Scheduled task with this Source and Name already exists."
        in response.content.decode()
    )


def test_posting_add_with_invalid_cron_shows_pg_crons_message(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {**CHANGE_PAYLOAD, "name": "badcron", "cron": "1 hour"})
    # behavioral: re-rendered with errors, not saved (the exact pg_cron message is
    # asserted in full by the validator harness's form subject, test_cron.py)
    assert response.status_code == 200
    assert not ScheduledTask.objects.filter(name="badcron").exists()


def test_settings_schedule_detail_is_readonly(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    pk = ScheduledTask.objects.get(name="hourly").pk  # a settings row
    client.force_login(admin_user)
    response = client.get(get_change_url(pk))
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    assert soup.select_one('input[name="cron"]') is None  # read-only, not editable
    assert "reports" in response.content.decode()  # queue option column rendered


def test_admin_schedule_edit_form_cron_editable_name_immutable(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**CHANGE_PAYLOAD, "name": "editable"})
    pk = ScheduledTask.objects.get(name="editable").pk
    response = client.get(get_change_url(pk))
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    assert (
        soup.select_one('input[name="cron"]') is not None
    )  # editable single-line input
    assert soup.select_one('[name="name"]') is None  # immutable on edit (not a field)


def test_posting_edit_reschedules_the_job_with_the_new_cron(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**CHANGE_PAYLOAD, "name": "reschedule", "cron": "0 3 * * *"})
    pk = ScheduledTask.objects.get(name="reschedule").pk

    response = client.post(
        get_change_url(pk),
        {**CHANGE_PAYLOAD, "task": "tests.tasks.add", "cron": "30 6 * * *"},
    )
    assert response.status_code == 302
    job = ScheduledTask.pg_cron.get_job("reschedule", "a")
    assert job is not None
    _, schedule, _, _ = job
    assert schedule == "30 6 * * *"


def test_deleting_admin_schedule_via_admin_unschedules_the_job(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**CHANGE_PAYLOAD, "name": "deleteme"})
    pk = ScheduledTask.objects.get(name="deleteme").pk
    assert ScheduledTask.pg_cron.get_job("deleteme", "a") is not None

    delete_url = reverse("admin:django_absurd_pg_cron_scheduledtask_delete", args=[pk])
    response = client.post(delete_url, {"post": "yes"})
    assert response.status_code == 302
    assert not ScheduledTask.objects.filter(name="deleteme").exists()
    assert ScheduledTask.pg_cron.get_job("deleteme", "a") is None


def test_deleting_settings_schedule_via_admin_is_forbidden(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    pk = ScheduledTask.objects.get(name="hourly").pk  # a settings row
    client.force_login(admin_user)
    delete_url = reverse("admin:django_absurd_pg_cron_scheduledtask_delete", args=[pk])
    assert client.get(delete_url).status_code == 403
    assert ScheduledTask.objects.filter(name="hourly").exists()


def test_change_form_rejects_a_blank_queue(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    client.post(
        ADD,
        {
            "name": "needsqueue",
            "task": "tests.tasks.add",
            "cron": "0 3 * * *",
        },
    )
    row = ScheduledTask.objects.get(name="needsqueue")
    response = client.post(get_change_url(row.pk), {**CHANGE_PAYLOAD, "queue": ""})
    assert response.status_code == 200
    assert "This field is required." in response.content.decode()


def test_change_view_queue_is_a_dropdown_of_declared_queues(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)  # QUEUES: default, other, reports
    client.force_login(admin_user)
    pk = create_scheduled_task(client, name="queuedropdown")
    soup = BeautifulSoup(client.get(get_change_url(pk)).content, "html.parser")
    values = [o.get("value") for o in soup.select('select[name="queue"] option')]
    assert values == ["", "default", "other", "reports"]


def test_change_view_retry_kind_is_a_dropdown(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    pk = create_scheduled_task(client, name="retrykind")
    soup = BeautifulSoup(client.get(get_change_url(pk)).content, "html.parser")
    values = [o.get("value") for o in soup.select('select[name="retry_kind"] option')]
    assert values == ["", "exponential", "fixed", "none"]


def test_change_view_cancellation_fields_are_number_inputs(
    admin_user: User,
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    seed(settings)
    client.force_login(admin_user)
    pk = create_scheduled_task(client, name="cancellation")
    soup = BeautifulSoup(client.get(get_change_url(pk)).content, "html.parser")
    assert soup.select_one('input[name="cancellation_max_duration"]') is not None
    assert soup.select_one('input[name="cancellation_max_delay"]') is not None


def test_add_forbidden_for_staff_without_permission(
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
    staff_user: User,
) -> None:
    seed(settings)
    client.force_login(staff_user)
    assert client.get(ADD).status_code == 403


def test_add_allowed_for_staff_with_permission(
    client: Client,
    settings: "pytest_django.fixtures.SettingsWrapper",
    staff_user: User,
) -> None:
    staff_user.user_permissions.add(
        Permission.objects.get(
            codename="add_scheduledtask",
            content_type__app_label="django_absurd_pg_cron",
        )
    )
    seed(settings)
    client.force_login(staff_user)
    assert client.get(ADD).status_code == 200
