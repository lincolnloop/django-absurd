import uuid

import pytest
from django.contrib.admin.utils import quote
from django.core.management import call_command
from django.db import connections
from django.urls import reverse, reverse_lazy

from tests.test_admin.support import parse_html, result_rows

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_checkpoint_changelist")


def change_url(pk):
    return reverse("admin:django_absurd_checkpoint_change", args=[quote(pk)])


def insert_checkpoint(task_id, name, status="committed"):
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, %s)",
            [task_id, name, '{"n": 1}', status],
        )


def test_changelist(client, admin_user):
    call_command("absurd_sync_queues")
    insert_checkpoint(uuid.uuid4(), "cp1")
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    names = {
        r.select_one(".field-checkpoint_name").get_text(strip=True)
        for r in result_rows(soup)
    }
    assert "cp1" in names


def test_detail_with_nasty_name(client, admin_user):
    call_command("absurd_sync_queues")
    tid = uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, 'committed')",
            [tid, "step/a:b c", '{"x": 1}'],
        )
    client.force_login(admin_user)
    soup = parse_html(client.get(change_url(f"default:{tid}:step/a:b c")))
    name_field = soup.select_one(".field-checkpoint_name .readonly")
    assert name_field is not None
    assert name_field.get_text(strip=True) == "step/a:b c"
