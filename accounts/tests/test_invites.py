from datetime import timedelta
from smtplib import SMTPException
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.checks import run_checks
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import Membership, Workspace
from ..services import (
    INVITE_SALT,
    INVITES_PER_HOUR,
    invite_member,
    make_invite_token,
)


class InviteDeliveryTests(TestCase):
    """Invites are only reported as sent when the mail server accepted them."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="Parent1", password="TestPassword123!"
        )
        self.workspace = Workspace.objects.create(
            name="Reyes family", primary_admin=self.admin
        )
        # Production gives the primary admin a membership row too.
        Membership.objects.create(
            workspace=self.workspace,
            user=self.admin,
            role=Membership.Role.PARENT,
            invited_by=self.admin,
        )
        self.members_url = reverse(
            "accounts:manage_members", kwargs={"workspace_id": self.workspace.id}
        )
        self.client.force_login(self.admin)

    def invite(self, email="kid@example.com", role=Membership.Role.KID):
        return invite_member(
            workspace=self.workspace, invited_by=self.admin, email=email, role=role
        )

    def accept_url(self, token):
        return reverse("accounts:accept_invite", kwargs={"token": token})

    def resend_url(self, membership):
        return reverse(
            "accounts:resend_invite",
            kwargs={"workspace_id": self.workspace.id, "membership_id": membership.pk},
        )

    def test_success_message_only_after_the_email_is_sent(self):
        response = self.client.post(
            self.members_url,
            {"email": "mateo@example.com", "role": "parent"},
            follow=True,
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Join Reyes family on Syncmily")
        self.assertContains(response, "Invite emailed to mateo@example.com")

    def test_failed_email_says_so_and_keeps_the_link_on_the_page(self):
        with patch("accounts.services.send_mail", side_effect=SMTPException("refused")):
            response = self.client.post(
                self.members_url,
                {"email": "mateo@example.com", "role": "parent"},
                follow=True,
            )

        self.assertNotContains(response, "Invite emailed")
        self.assertContains(response, "go through. Copy")
        membership = Membership.objects.get(user__email="mateo@example.com")
        self.assertContains(response, f'id="invite-link-{membership.pk}"')

    def test_kid_can_be_invited_without_an_email(self):
        response = self.client.post(
            self.members_url, {"email": "", "role": "kid"}, follow=True
        )

        self.assertEqual(len(mail.outbox), 0)
        kid = Membership.objects.get(role=Membership.Role.KID)
        self.assertEqual(kid.user.email, "")
        self.assertContains(response, "Copy their invite link")
        self.assertContains(response, f'id="invite-link-{kid.pk}"')

    def test_parent_needs_an_email(self):
        response = self.client.post(self.members_url, {"email": "", "role": "parent"})

        self.assertContains(response, "Parents need an email address.")
        self.assertFalse(
            Membership.objects.filter(role=Membership.Role.PARENT)
            .exclude(user=self.admin)
            .exists()
        )

    def test_resend_replaces_the_link(self):
        membership = self.invite()
        Membership.objects.filter(pk=membership.pk).update(
            invited_at=membership.invited_at - timedelta(days=1)
        )
        membership.refresh_from_db()
        old_token = make_invite_token(membership)

        self.client.post(self.resend_url(membership))

        membership.refresh_from_db()
        self.assertEqual(self.client.get(self.accept_url(old_token)).status_code, 400)
        self.assertEqual(
            self.client.get(self.accept_url(make_invite_token(membership))).status_code,
            200,
        )
        self.assertEqual(
            Membership.objects.filter(user__email="kid@example.com").count(), 1
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_links_from_before_this_change_stop_working(self):
        membership = self.invite()
        legacy_token = signing.dumps({"membership": membership.pk}, salt=INVITE_SALT)

        self.assertEqual(
            self.client.get(self.accept_url(legacy_token)).status_code, 400
        )

    def test_only_the_primary_admin_can_resend(self):
        membership = self.invite()
        other = get_user_model().objects.create_user(
            username="Mateo", password="TestPassword123!"
        )
        self.client.force_login(other)

        self.assertEqual(self.client.post(self.resend_url(membership)).status_code, 403)

    def test_accepted_invites_cannot_be_resent(self):
        membership = self.invite()
        Membership.objects.filter(pk=membership.pk).update(
            accepted_at=membership.invited_at
        )

        self.assertEqual(self.client.post(self.resend_url(membership)).status_code, 404)

    def test_invites_are_rate_limited(self):
        for n in range(INVITES_PER_HOUR):
            self.invite(email=f"kid{n}@example.com")

        response = self.client.post(
            self.members_url, {"email": "one-more@example.com", "role": "kid"}
        )

        self.assertContains(response, "a lot of invites in the last hour")
        self.assertFalse(
            Membership.objects.filter(user__email="one-more@example.com").exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_primary_admin_is_listed_once(self):
        response = self.client.get(self.members_url)

        # The redesigned Members page lists the family once, the admin included.
        self.assertContains(response, "In the family (1)")
        self.assertContains(response, "Parent, admin", count=1)


class EmailDeployCheckTests(TestCase):

    def ids(self):
        return {
            c.id
            for c in run_checks(include_deployment_checks=True)
            if c.id.startswith("accounts.")
        }

    @override_settings(
        DEBUG=False, EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend"
    )
    def test_console_backend_fails_the_deploy_check(self):
        self.assertIn("accounts.E001", self.ids())

    @override_settings(
        DEBUG=False,
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        DEFAULT_FROM_EMAIL="Syncmily <no-reply@syncmily.app>",
    )
    def test_smtp_backend_passes(self):
        self.assertEqual(self.ids(), set())

    @override_settings(
        DEBUG=False, EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend"
    )
    def test_local_from_address_warns(self):
        self.assertIn("accounts.W001", self.ids())

    @override_settings(
        DEBUG=True, EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend"
    )
    def test_development_is_left_alone(self):
        self.assertEqual(self.ids(), set())
