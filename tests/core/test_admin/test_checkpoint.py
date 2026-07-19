import typing as t
import uuid

import pytest
from django.contrib.admin.utils import quote
from django.contrib.auth.models import AbstractBaseUser, User
from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.urls import reverse, reverse_lazy

from tests.core.test_admin.utils import parse_html, result_rows

if t.TYPE_CHECKING:
    from bs4 import Tag

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_checkpoint_changelist")


def change_url(pk: str) -> str:
    return reverse("admin:django_absurd_checkpoint_change", args=[quote(pk)])


def insert_checkpoint(task_id: uuid.UUID, name: str, status: str = "committed") -> None:
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, %s)",
            [task_id, name, '{"n": 1}', status],
        )


def test_changelist(client: Client, admin_user: AbstractBaseUser) -> None:
    call_command("absurd_sync_queues")
    insert_checkpoint(uuid.uuid4(), "cp1")
    client.force_login(t.cast("User", admin_user))
    response = client.get(CHANGELIST)
    soup = parse_html(response)
    names = {
        t.cast("Tag", r.select_one(".field-checkpoint_name")).get_text(strip=True)
        for r in result_rows(soup)
    }
    assert "cp1" in names


def test_detail_with_nasty_name(client: Client, admin_user: AbstractBaseUser) -> None:
    call_command("absurd_sync_queues")
    tid = uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, 'committed')",
            [tid, "step/a:b c", '{"x": 1}'],
        )
    client.force_login(t.cast("User", admin_user))
    response = client.get(change_url(f"default:{tid}:step/a:b c"))
    soup = parse_html(response)
    name_field = soup.select_one(".field-checkpoint_name .readonly")
    assert name_field is not None
    assert name_field.get_text(strip=True) == "step/a:b c"
