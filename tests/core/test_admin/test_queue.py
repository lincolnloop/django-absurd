import pytest
from django.contrib.admin.utils import quote
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse, reverse_lazy

from tests.core.test_admin.utils import parse_html, result_rows

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_queue_changelist")


def change_url(pk: str) -> str:
    return reverse("admin:django_absurd_queue_change", args=[quote(pk)])


def test_changelist_lists_declared_queues(admin_user: User, client: Client) -> None:
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    names = []
    for r in result_rows(soup):
        elem = r.select_one(".field-queue_name")
        assert elem is not None
        names.append(elem.get_text(strip=True))
    assert names == ["default", "other", "reports"]


def test_change_page_shows_queue(admin_user: User, client: Client) -> None:
    client.force_login(admin_user)
    detail = parse_html(client.get(change_url("default")))
    name_elem = detail.select_one(".field-queue_name .readonly")
    assert name_elem is not None
    assert name_elem.get_text(strip=True) == "default"
