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

CHANGELIST = reverse_lazy("admin:django_absurd_wait_changelist")


def change_url(pk: str) -> str:
    return reverse("admin:django_absurd_wait_change", args=[quote(pk)])


def test_changelist_and_composite_detail(
    client: Client, admin_user: AbstractBaseUser
) -> None:
    call_command("absurd_sync_queues")
    rid, tid = uuid.uuid4(), uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."w_default" (task_id, run_id, step_name, event_name)'
            " VALUES (%s, %s, %s, %s)",
            [tid, rid, "wait/step:1", "evt"],
        )
    client.force_login(t.cast("User", admin_user))
    response = client.get(CHANGELIST)
    soup = parse_html(response)
    steps = {
        t.cast("Tag", r.select_one(".field-step_name")).get_text(strip=True)
        for r in result_rows(soup)
    }
    assert "wait/step:1" in steps

    # composite-PK detail (queue:run_id:step_name) with a nasty step_name
    detail_response = client.get(change_url(f"default:{rid}:wait/step:1"))
    detail = parse_html(detail_response)
    step_name_elem = detail.select_one(".field-step_name .readonly")
    assert step_name_elem is not None
    assert step_name_elem.get_text(strip=True) == "wait/step:1"
