"""Typing a member's colour as a hex code.

Hex codes are typed beside each colour picker; only the picker is sent, so the
server keeps its own check.
"""

from django.test import TestCase
from django.urls import reverse

from accounts.models import MemberColor

from .test_paint_pages import FamilyMixin


class HexColourTests(FamilyMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.ana)
        self.save_url = reverse("events:member_colors", args=[self.workspace.pk])

    def test_family_page_and_header_offer_a_hex_field_per_member(self):
        response = self.client.get(
            reverse("accounts:workspace_detail", args=[self.workspace.pk])
        )
        # Family page panel, the header's Colors menu and the phone account menu:
        # one each per editable member (Ana, Mateo, Lucía).
        self.assertContains(response, "data-hex hidden", count=9)
        self.assertContains(response, 'pattern="#?[0-9A-Fa-f]{6}"')
        self.assertContains(response, "colour as a hex code")
        # The hex field has no name: only the picker is sent.
        self.assertNotRegex(response.content.decode(), r'class="hex"[^>]*\bname=')
        # The two header copies keep their ids apart, and each name opens its picker.
        for place in ("menu", "acct"):
            self.assertContains(response, f'for="{place}-colour-{self.mateo.pk}"')
            self.assertContains(
                response, f'id="{place}-colour-{self.mateo.pk}"', count=1
            )

    def test_valid_hex_saves_and_a_repeat_changes_nothing(self):
        data = {"next": "/", f"color-{self.lucia.pk}": "#4a6fd8"}
        self.client.post(self.save_url, data)
        self.client.post(self.save_url, data)
        colours = MemberColor.objects.filter(workspace=self.workspace, user=self.lucia)
        self.assertEqual(colours.count(), 1)
        self.assertEqual(colours.get().color.lower(), "#4a6fd8")

    def test_bad_hex_is_refused_by_the_server(self):
        self.client.post(
            self.save_url, {"next": "/", f"color-{self.lucia.pk}": "red;}"}
        )
        self.assertFalse(
            MemberColor.objects.filter(
                workspace=self.workspace, user=self.lucia
            ).exists()
        )
