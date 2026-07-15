from html import unescape

import pytest
from bs4 import BeautifulSoup
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.urls import reverse, reverse_lazy

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_changelist")
ADD = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_add")

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "QUEUES": {"default": {}, "other": {}, "reports": {}},
            "SCHEDULER": "pg_cron",
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

ADD_PAYLOAD = {
    "alias": "default",
    "task": "tests.tasks.add",
    "queue": "",
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


def seed(settings):
    settings.TASKS = TASKS
    call_command("absurd_sync_queues")
    sync_crons(get_absurd_backends()["default"])


def rows(response):
    soup = BeautifulSoup(response.content, "html.parser")
    return soup.select("#result_list tbody tr")


def change_url(pk):
    return reverse("admin:django_absurd_pg_cron_scheduledtask_change", args=[pk])


def test_changelist_renders_one_row_per_schedule(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    assert len(rows(client.get(CHANGELIST))) == 2


def test_changelist_shows_expected_columns(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    body = client.get(CHANGELIST).content.decode()
    assert "nightly" in body
    assert "0 2 * * *" in body
    assert "tests.tasks.add" in body


def test_queue_filter_renders_and_narrows(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(CHANGELIST).content, "html.parser")
    assert soup.select_one('#changelist-filter a[href*="reports"]') is not None
    narrowed = rows(client.get(CHANGELIST, {"queue__exact": "reports"}))
    assert len(narrowed) == 1
    assert "hourly" in narrowed[0].get_text()


def test_search_by_name_narrows(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    assert len(rows(client.get(CHANGELIST, {"q": "nightly"}))) == 1


def test_add_link_present_and_add_view_renders(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    changelist = client.get(CHANGELIST)
    assert changelist.status_code == 200
    soup = BeautifulSoup(changelist.content, "html.parser")
    assert soup.select_one(".object-tools a.addlink") is not None
    add = client.get(ADD)
    assert add.status_code == 200


def test_add_view_backend_field_offers_only_pg_cron_backends(
    settings, client, admin_user
):
    seed(settings)  # a single pg_cron backend "default"
    client.force_login(admin_user)
    response = client.get(ADD)
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    options = [
        o.get("value")
        for o in soup.select('select[name="alias"] option')
        if o.get("value")
    ]
    assert options == ["default"]


def test_add_view_alias_field_labeled_alias(settings, client, admin_user):
    # The field is model-named "alias" everywhere; the add form must not relabel it to
    # "Backend" — the readonly change view shows "Alias", so both must agree.
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    label = soup.select_one('label[for="id_alias"]')
    assert label is not None
    assert label.get_text(strip=True).rstrip(":") == "Alias"


def test_add_view_cron_help_renders_pg_cron_link_as_html(settings, client, admin_user):
    # the cron field's help text embeds an <a> to the pg_cron docs; Django form
    # help_text is not auto-escaped, so it must reach the page as a real anchor (an
    # escaped &lt;a&gt; would be inert text, not a clickable link). BeautifulSoup finding
    # it as an <a> element inside the cron row proves it rendered as HTML.
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    link = soup.select_one(
        '.field-cron .help a[href="https://github.com/citusdata/pg_cron"]'
    )
    assert link is not None
    assert link.get_text() == "pg_cron"


def test_add_view_prefills_default_max_attempts(settings, client, admin_user):
    # max_attempts defaults to 5 in the form, so leaving it be gives a bounded schedule;
    # infinite retries require deliberately clearing the field.
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    field = soup.select_one('input[name="max_attempts"]')
    assert field is not None
    assert field.get("value") == "5"


def test_posting_add_creates_admin_schedule_and_schedules_job(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {**ADD_PAYLOAD, "name": "fromadmin"})
    assert response.status_code == 302
    assert ScheduledTask.objects.get(name="fromadmin").source == "a"
    assert ScheduledTask.pg_cron.get_job("default", "fromadmin", "a") is not None


def test_posting_add_with_blank_args_kwargs_falls_back_to_defaults(
    settings, client, admin_user
):
    # Blank args/kwargs in the form must fall back to the field defaults ([] / {}),
    # not save NULL into the NOT NULL JSON columns (which surfaced as HTTP 500).
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD, {**ADD_PAYLOAD, "name": "blankjson", "args": "", "kwargs": ""}
    )
    assert response.status_code == 302
    row = ScheduledTask.objects.get(name="blankjson")
    assert row.args == []
    assert row.kwargs == {}


def test_posting_add_with_tampered_source_is_forced_to_admin(
    settings, client, admin_user
):
    # source is a hidden, pinned field: a crafted POST setting source="s" (settings)
    # must not create a settings-owned row via the writable admin.
    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {**ADD_PAYLOAD, "name": "tamper", "source": "s"})
    assert response.status_code == 302
    assert ScheduledTask.objects.get(name="tamper").source == "a"


def test_editing_over_long_name_row_is_form_error_not_500(settings, client, admin_user):
    # A row whose name overflows the 63-byte jobname budget (created out-of-band, so it
    # skipped full_clean) must surface a form error on edit, not HTTP 500 — clean()
    # keys the jobname-length error to NON_FIELD_ERRORS, not the read-only "name" field.
    seed(settings)
    client.force_login(admin_user)
    row = ScheduledTask.objects.create(
        source="a",
        alias="default",
        name="x" * 60,
        task="tests.tasks.add",
        cron="0 3 * * *",
    )
    response = client.post(change_url(row.pk), {**ADD_PAYLOAD, "cron": "0 4 * * *"})
    assert response.status_code == 200
    content = response.content.decode()
    assert "job name exceeds 63 bytes (composed name" in content
    assert "Postgres silently truncates longer names)." in content


def test_editing_blank_args_kwargs_falls_back_to_defaults(settings, client, admin_user):
    # queue="reports" (declared) so the edit exercises the "stored queue is still a
    # valid choice" path (no injection) alongside the blank args/kwargs fallback.
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**ADD_PAYLOAD, "name": "editblank", "queue": "reports"})
    row = ScheduledTask.objects.get(name="editblank")
    response = client.post(
        change_url(row.pk),
        {**ADD_PAYLOAD, "queue": "reports", "args": "", "kwargs": ""},
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.args == []
    assert row.kwargs == {}


def test_change_form_renders_stored_queue_absent_from_choices(
    settings, client, admin_user
):
    # A stored queue no longer declared has dropped out of the field's choices; the
    # change form must still render the real value (injected back) rather than silently
    # offering a different one.
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}, "SCHEDULER": "pg_cron"},
        }
    }
    row = ScheduledTask.objects.create(
        source="a",
        alias="default",
        name="stale",
        task="tests.tasks.add",
        queue="reports",
        cron="0 3 * * *",
    )
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(change_url(row.pk)).content, "html.parser")
    values = [o.get("value") for o in soup.select('select[name="queue"] option')]
    assert "reports" in values


def test_posting_duplicate_admin_name_is_form_error_not_500(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**ADD_PAYLOAD, "name": "dup"})
    response = client.post(ADD, {**ADD_PAYLOAD, "name": "dup"})
    assert response.status_code == 200  # re-rendered with a form error, not HTTP 500
    assert ScheduledTask.objects.filter(source="a", name="dup").count() == 1
    assert (
        "Scheduled task with this Source, Alias and Name already exists."
        in response.content.decode()
    )


def test_posting_add_with_invalid_cron_shows_pg_crons_message(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    response = client.post(ADD, {**ADD_PAYLOAD, "name": "badcron", "cron": "1 hour"})
    # behavioral: re-rendered with errors, not saved (the exact pg_cron message is
    # asserted in full by the validator harness's form subject, test_cron.py)
    assert response.status_code == 200
    assert not ScheduledTask.objects.filter(name="badcron").exists()


def test_settings_schedule_detail_is_readonly(settings, client, admin_user):
    seed(settings)
    pk = ScheduledTask.objects.get(name="hourly").pk  # a settings row
    client.force_login(admin_user)
    response = client.get(change_url(pk))
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    assert soup.select_one('input[name="cron"]') is None  # read-only, not editable
    assert "reports" in response.content.decode()  # queue option column rendered


def test_admin_schedule_edit_form_cron_editable_name_immutable(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**ADD_PAYLOAD, "name": "editable"})
    pk = ScheduledTask.objects.get(name="editable").pk
    response = client.get(change_url(pk))
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    assert (
        soup.select_one('input[name="cron"]') is not None
    )  # editable single-line input
    assert soup.select_one('[name="name"]') is None  # immutable on edit (not a field)


def test_posting_edit_reschedules_the_job_with_the_new_cron(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**ADD_PAYLOAD, "name": "reschedule", "cron": "0 3 * * *"})
    pk = ScheduledTask.objects.get(name="reschedule").pk

    response = client.post(
        change_url(pk), {**ADD_PAYLOAD, "task": "tests.tasks.add", "cron": "30 6 * * *"}
    )
    assert response.status_code == 302
    _, schedule, _, _ = ScheduledTask.pg_cron.get_job("default", "reschedule", "a")
    assert schedule == "30 6 * * *"


def test_deleting_admin_schedule_via_admin_unschedules_the_job(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**ADD_PAYLOAD, "name": "deleteme"})
    pk = ScheduledTask.objects.get(name="deleteme").pk
    assert ScheduledTask.pg_cron.get_job("default", "deleteme", "a") is not None

    delete_url = reverse("admin:django_absurd_pg_cron_scheduledtask_delete", args=[pk])
    response = client.post(delete_url, {"post": "yes"})
    assert response.status_code == 302
    assert not ScheduledTask.objects.filter(name="deleteme").exists()
    assert ScheduledTask.pg_cron.get_job("default", "deleteme", "a") is None


def test_deleting_settings_schedule_via_admin_is_forbidden(
    settings, client, admin_user
):
    seed(settings)
    pk = ScheduledTask.objects.get(name="hourly").pk  # a settings row
    client.force_login(admin_user)
    delete_url = reverse("admin:django_absurd_pg_cron_scheduledtask_delete", args=[pk])
    assert client.get(delete_url).status_code == 403
    assert ScheduledTask.objects.filter(name="hourly").exists()


def test_editing_admin_schedule_after_backend_flip_is_form_error_not_500(
    settings, client, admin_user
):
    # an admin row whose backend later switched off pg_cron must surface a form
    # error on edit, not crash (the alias error routes to NON_FIELD_ERRORS since
    # alias is a read-only field on the change form)
    seed(settings)
    client.force_login(admin_user)
    client.post(ADD, {**ADD_PAYLOAD, "name": "flipme"})
    pk = ScheduledTask.objects.get(name="flipme").pk

    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}, "SCHEDULER": "beat"},
        }
    }
    response = client.post(
        change_url(pk),
        {
            "task": "tests.tasks.add",
            "queue": "",
            "cron": "0 4 * * *",
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
        },
    )
    assert response.status_code == 200  # form error, not HTTP 500
    assert "backend 'default' is not a configured pg_cron backend." in unescape(
        response.content.decode()
    )


def test_add_view_queue_is_a_dropdown_of_declared_queues(settings, client, admin_user):
    seed(settings)  # QUEUES: default, other, reports
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    values = [o.get("value") for o in soup.select('select[name="queue"] option')]
    assert values == ["", "default", "other", "reports"]


def test_add_view_retry_kind_is_a_dropdown(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    values = [o.get("value") for o in soup.select('select[name="retry_kind"] option')]
    assert values == ["", "exponential", "fixed", "none"]


def test_add_view_cancellation_fields_are_number_inputs(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    assert soup.select_one('input[name="cancellation_max_duration"]') is not None
    assert soup.select_one('input[name="cancellation_max_delay"]') is not None


def test_add_forbidden_for_staff_without_permission(settings, client, staff_user):
    seed(settings)
    client.force_login(staff_user)
    assert client.get(ADD).status_code == 403


def test_add_allowed_for_staff_with_permission(settings, client, staff_user):
    staff_user.user_permissions.add(
        Permission.objects.get(
            codename="add_scheduledtask",
            content_type__app_label="django_absurd_pg_cron",
        )
    )
    seed(settings)
    client.force_login(staff_user)
    assert client.get(ADD).status_code == 200
