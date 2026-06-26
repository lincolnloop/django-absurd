import pytest
from django.contrib.admin.utils import quote
from django.core.management import call_command
from django.db import connections
from django.urls import reverse, reverse_lazy

from tests.test_admin.support import parse_html, result_rows

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_event_changelist")


def change_url(pk):
    return reverse("admin:django_absurd_event_change", args=[quote(pk)])


def test_changelist_and_detail(client, admin_user):
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."e_default" (event_name, payload) VALUES (%s, %s)',
            ["order.shipped", '{"id": 1}'],
        )
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    names = {
        r.select_one(".field-event_name").get_text(strip=True)
        for r in result_rows(soup)
    }
    assert "order.shipped" in names

    detail = parse_html(client.get(change_url("default:order.shipped")))
    assert (
        detail.select_one(".field-event_name .readonly").get_text(strip=True)
        == "order.shipped"
    )
