from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ..forms import UsernameChangeForm, WorkspaceForm
from ..models import Membership, Workspace
from ..services import invite_member
from .test_two_step import app_code

User = get_user_model()
PASSWORD = "TestPassword123!"


def make_family(admin_name="Parent1"):
    admin = User.objects.create_user(username=admin_name, password=PASSWORD)
    workspace = Workspace.objects.create(name="Reyes family", primary_admin=admin)
    Membership.objects.create(
        workspace=workspace,
        user=admin,
        role=Membership.Role.PARENT,
        invited_by=admin,
    )
    return admin, workspace


@override_settings(RATELIMIT_ENABLED=True)
class RateLimitedEndpointTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_signup_is_limited_per_ip(self):
        codes = [self.client.post(reverse("accounts:signup"), {}).status_code]
        for _ in range(5):
            codes.append(self.client.post(reverse("accounts:signup"), {}).status_code)
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5], 429)

    def test_login_post_is_limited_per_ip(self):
        codes = [
            self.client.post(
                reverse("accounts:login"), {"username": f"u{n}", "password": "x"}
            ).status_code
            for n in range(31)
        ]
        self.assertIn(429, codes)
        self.assertEqual(codes[-1], 429)

    def test_create_workspace_is_limited_per_user(self):
        user = User.objects.create_user(username="solo", password=PASSWORD)
        self.client.force_login(user)
        codes = [
            self.client.post(reverse("accounts:create_workspace"), {"name": ""})
            for _ in range(6)
        ]
        self.assertEqual(codes[-1].status_code, 429)

    def test_two_step_verify_post_is_limited(self):
        codes = [
            self.client.post(reverse("accounts:two_step_verify"), {"code": "1"})
            for _ in range(25)
        ]
        self.assertEqual(codes[-1].status_code, 429)

    def test_password_reset_post_is_limited(self):
        url = reverse("accounts:password_reset")
        codes = [
            self.client.post(url, {"email": "a@example.com"}).status_code
            for _ in range(6)
        ]
        self.assertEqual(codes[-1], 429)


@override_settings(RATELIMIT_ENABLED=True)
class InviteSendCapTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin, self.workspace = make_family()
        self.client.force_login(self.admin)
        self.members_url = reverse(
            "accounts:manage_members", kwargs={"workspace_id": self.workspace.id}
        )

    def test_resends_count_against_the_hourly_cap(self):
        membership = invite_member(
            workspace=self.workspace,
            invited_by=self.admin,
            email="kid@example.com",
            role=Membership.Role.KID,
        )
        url = reverse(
            "accounts:resend_invite",
            kwargs={"workspace_id": self.workspace.id, "membership_id": membership.pk},
        )
        for _ in range(40):
            self.client.post(url)
        self.assertLessEqual(len(mail.outbox), 20)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "a lot of invites")

    def test_daily_cap_applies_across_hours(self):
        from datetime import timedelta

        from django.utils import timezone

        for n in range(60):
            invite_member(
                workspace=self.workspace,
                invited_by=self.admin,
                email=f"k{n}@example.com",
                role=Membership.Role.KID,
            )
        Membership.objects.filter(invited_by=self.admin).exclude(
            user=self.admin
        ).update(invited_at=timezone.now() - timedelta(hours=3))
        cache.clear()
        response = self.client.post(
            self.members_url, {"email": "late@example.com", "role": "kid"}
        )
        self.assertContains(response, "a lot of invites")


class GhostFamilyTests(TestCase):
    def test_user_with_a_family_cannot_create_another(self):
        admin, _ = make_family()
        self.client.force_login(admin)
        response = self.client.post(
            reverse("accounts:create_workspace"), {"name": "Second"}
        )
        self.assertRedirects(
            response, reverse("accounts:dashboard"), fetch_redirect_response=False
        )
        self.assertEqual(Workspace.objects.count(), 1)

    def test_member_cannot_create_a_family(self):
        admin, workspace = make_family()
        membership = invite_member(
            workspace=workspace,
            invited_by=admin,
            email="",
            role=Membership.Role.KID,
        )
        self.client.force_login(membership.user)
        self.client.post(reverse("accounts:create_workspace"), {"name": "Mine"})
        self.assertEqual(Workspace.objects.count(), 1)

    def test_double_submit_creates_one_family(self):
        user = User.objects.create_user(username="solo", password=PASSWORD)
        self.client.force_login(user)
        for _ in range(2):
            self.client.post(reverse("accounts:create_workspace"), {"name": "Solo"})
        self.assertEqual(Workspace.objects.filter(primary_admin=user).count(), 1)


class FamilyNameTests(TestCase):
    def test_control_characters_are_stripped(self):
        form = WorkspaceForm({"name": "Evil\nBcc: x@y.z‮  family"})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["name"], "Evil Bcc: x@y.z family")

    def test_only_invisible_characters_is_rejected(self):
        self.assertFalse(WorkspaceForm({"name": "​‮ \n"}).is_valid())

    def test_stored_bad_name_does_not_crash_the_invite(self):
        admin, workspace = make_family()
        Workspace.objects.filter(pk=workspace.pk).update(name="Evil\nBcc: x@y.z")
        self.client.force_login(admin)
        response = self.client.post(
            reverse("accounts:manage_members", kwargs={"workspace_id": workspace.id}),
            {"email": "kid@example.com", "role": "kid"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("\n", mail.outbox[0].subject)

    def test_bad_header_error_becomes_a_warning_not_a_500(self):
        from unittest.mock import patch

        from django.core.mail import BadHeaderError

        admin, workspace = make_family()
        self.client.force_login(admin)
        with patch(
            "accounts.views.send_invitation_email", side_effect=BadHeaderError("x")
        ):
            response = self.client.post(
                reverse(
                    "accounts:manage_members", kwargs={"workspace_id": workspace.id}
                ),
                {"email": "kid@example.com", "role": "kid"},
                follow=True,
            )
        self.assertContains(response, "didn")


class ReservedUsernameTests(TestCase):
    def test_username_change_rejects_reserved_names(self):
        user = User.objects.create_user(username="maya", password=PASSWORD)
        for name in ("Admin", "parent1", "KID3", "Support", "syncmily", "Parent"):
            with self.subTest(name=name):
                form = UsernameChangeForm({"username": name}, instance=user)
                self.assertFalse(form.is_valid())
                self.assertIn("reserved", str(form.errors))

    def test_signup_rejects_reserved_names(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "username": "Admin",
                "email": "a@example.com",
                "password1": "BlueKite2026!",
                "password2": "BlueKite2026!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="Admin").exists())

    def test_generated_invite_names_still_work(self):
        admin, workspace = make_family("Boss")
        membership = invite_member(
            workspace=workspace,
            invited_by=admin,
            email="",
            role=Membership.Role.KID,
        )
        self.assertEqual(membership.user.username, "Kid1")


class PendingInviteSignupTests(TestCase):
    def test_invited_email_is_pointed_to_the_invite_link(self):
        admin, workspace = make_family()
        invite_member(
            workspace=workspace,
            invited_by=admin,
            email="mateo@example.com",
            role=Membership.Role.PARENT,
        )
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "username": "mateo",
                "email": "Mateo@example.com",
                "password1": "BlueKite2026!",
                "password2": "BlueKite2026!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invite link")
        self.assertNotContains(response, "Sign in instead")
        self.assertFalse(User.objects.filter(username="mateo").exists())

    def test_active_account_keeps_the_sign_in_message(self):
        User.objects.create_user(
            username="maya", email="maya@example.com", password=PASSWORD
        )
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "username": "maya2",
                "email": "maya@example.com",
                "password1": "BlueKite2026!",
                "password2": "BlueKite2026!",
            },
        )
        self.assertContains(response, "Sign in instead")


class PasswordResetTests(TestCase):
    def test_full_reset_flow(self):
        User.objects.create_user(
            username="maya", email="maya@example.com", password=PASSWORD
        )
        response = self.client.post(
            reverse("accounts:password_reset"), {"email": "maya@example.com"}
        )
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        link = next(
            word for word in mail.outbox[0].body.split() if "/accounts/reset/" in word
        )
        response = self.client.get(link, follow=True)
        self.assertEqual(response.status_code, 200)
        post_url = response.request["PATH_INFO"]
        response = self.client.post(
            post_url,
            {"new_password1": "GreenKite2027!", "new_password2": "GreenKite2027!"},
        )
        self.assertRedirects(response, reverse("accounts:password_reset_complete"))
        self.assertTrue(
            User.objects.get(username="maya").check_password("GreenKite2027!")
        )

    def test_unknown_email_gets_the_same_answer_and_no_mail(self):
        response = self.client.post(
            reverse("accounts:password_reset"), {"email": "nobody@example.com"}
        )
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_login_links_to_reset_and_lockout_copy_is_fixed(self):
        self.assertContains(
            self.client.get(reverse("accounts:login")),
            reverse("accounts:password_reset"),
        )
        from django.template.loader import render_to_string

        text = render_to_string("accounts/locked_out.html")
        self.assertNotIn("Ask the parent who runs your family to help", text)
        self.assertIn(reverse("accounts:password_reset"), text)


class ErrorPageTests(TestCase):
    def test_404_uses_the_styled_page(self):
        response = self.client.get("/no/such/page/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")

    def test_403_uses_the_styled_page(self):
        admin, workspace = make_family()
        other = User.objects.create_user(username="other", password=PASSWORD)
        self.client.force_login(other)
        response = self.client.get(
            reverse("accounts:workspace_detail", kwargs={"workspace_id": workspace.id})
        )
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "403.html")
        self.assertContains(response, reverse("accounts:dashboard"), status_code=403)

    def test_500_renders_without_context_or_database(self):
        from django.template.loader import get_template

        html = get_template("500.html").render()
        self.assertIn(reverse("accounts:dashboard"), html)


class SessionsAfterTwoStepTests(TestCase):
    def test_other_sessions_are_signed_out_when_two_step_turns_on(self):
        user = User.objects.create_user(username="maya", password=PASSWORD)
        other = Client()
        other.force_login(user)
        self.client.force_login(user)
        self.client.get(reverse("accounts:two_step_setup"))
        from django_otp.plugins.otp_totp.models import TOTPDevice

        device = TOTPDevice.objects.get(user=user)
        old_key = self.client.session.session_key
        self.client.post(reverse("accounts:two_step_setup"), {"code": app_code(device)})
        self.assertNotEqual(self.client.session.session_key, old_key)
        self.assertEqual(Session.objects.count(), 1)
        response = other.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)
