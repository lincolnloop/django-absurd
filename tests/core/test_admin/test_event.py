import typing as t

import pytest
from django.contrib.admin.utils import quote
from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.urls import reverse, reverse_lazy

from tests.core.test_admin.utils import parse_html, result_rows

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_event_changelist")


def change_url(pk: str) -> str:
    return reverse("admin:django_absurd_event_change", args=[quote(pk)])


def test_changelist_and_detail(client: Client, admin_user: t.Any) -> None:
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."e_default" (event_name, payload) VALUES (%s, %s)',
            ["order.shipped", '{"id": 1}'],
        )
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    names = set()
    for r in result_rows(soup):
        elem = r.select_one(".field-event_name")
        assert elem is not None
        names.add(elem.get_text(strip=True))
    assert "order.shipped" in names

    response = client.get(change_url("default:order.shipped"))
    detail = parse_html(response)
    name_elem = detail.select_one(".field-event_name .readonly")
    assert name_elem is not None
    assert name_elem.get_text(strip=True) == "order.shipped"
