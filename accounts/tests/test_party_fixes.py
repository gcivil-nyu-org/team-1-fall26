from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Membership, Workspace


class NoFamilyScheduleTests(TestCase):
    """New sign-ups reach the schedule before they have a family."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.newbie = User.objects.create_user(username="newbie", password="x")
        cls.parent = User.objects.create_user(username="parent", password="x")
        cls.kid = User.objects.create_user(username="kid", password="x")
        workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.parent)
        Membership.objects.create(
            workspace=workspace,
            user=cls.kid,
            role=Membership.Role.KID,
            invited_by=cls.parent,
            accepted_at=timezone.now(),
        )

    def test_schedule_pages_say_to_create_or_join_a_family(self):
        self.client.force_login(self.newbie)

        for view in ("day", "week", "upcoming"):
            with self.subTest(view=view):
                response = self.client.get(reverse("events:list"), {"view": view})
                self.assertContains(response, "You’re not in a family yet")
                self.assertContains(response, reverse("accounts:create_workspace"))

    def test_family_members_do_not_see_it(self):
        for user in (self.parent, self.kid):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("events:list"), {"view": "day"})
                self.assertNotContains(response, "You’re not in a family yet")


class SignOutPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="ana", password="x")

    def test_opening_the_address_asks_instead_of_405(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("accounts:logout"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign out of Syncmily?")
        self.assertIn("_auth_user_id", self.client.session)

    def test_the_button_signs_out(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("accounts:logout"))

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signed_out_visitors_go_to_sign_in(self):
        response = self.client.get(reverse("accounts:logout"))

        self.assertRedirects(
            response, reverse("accounts:login"), fetch_redirect_response=False
        )
