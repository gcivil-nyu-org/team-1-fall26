"""Account identity hardening (package p04): email ownership, invites, usernames,
two-step secrets at rest and the admin's code step."""

import re
import threading
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import otp_storage, services
from accounts.models import (
    EmailAddress,
    GoogleIdentity,
    Membership,
    UsernameHistory,
    Workspace,
)

User = get_user_model()
PASSWORD = "Identity-Test-2026!"


def wait_for_mail():
    for worker in threading.enumerate():
        if worker is not threading.current_thread() and worker.daemon:
            worker.join(2)


def sign_up(client, username="maya", email="maya@example.com"):
    return client.post(
        reverse("accounts:signup"),
        {
            "username": username,
            "email": email,
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )


class EmailVerificationTests(TestCase):
    def test_sign_up_sends_a_link_that_confirms_the_address_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            sign_up(self.client)
        wait_for_mail()
        user = User.objects.get(username="maya")
        self.assertFalse(services.email_verified(user))
        self.assertEqual(mail.outbox[0].to, ["maya@example.com"])
        link = re.search(
            r"https?://\S+/accounts/verify-email/\S+/", mail.outbox[0].body
        )
        path = link.group(0).split("testserver", 1)[1]

        # Opening the link only shows a button; mail scanners can't confirm it.
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertFalse(services.email_verified(user))
        self.client.post(path)
        self.assertTrue(services.email_verified(user))
        self.assertEqual(self.client.post(path).status_code, 400)

    def test_a_new_link_replaces_the_old_one(self):
        sign_up(self.client)
        user = User.objects.get(username="maya")
        old = services.email_verification_token(EmailAddress.objects.get(user=user))
        self.client.post(reverse("accounts:resend_verification"))
        url = reverse("accounts:verify_email", args=[old])
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertFalse(services.email_verified(user))

    def test_unverified_accounts_cannot_invite(self):
        sign_up(self.client)
        self.client.post(reverse("accounts:create_workspace"), {"name": "Reyes"})
        workspace = Workspace.objects.get(primary_admin__username="maya")
        response = self.client.post(
            reverse("accounts:manage_members", args=[workspace.pk]),
            {"email": "kid@example.com", "role": "kid"},
        )
        self.assertContains(response, "Confirm your email address")
        self.assertFalse(Membership.objects.filter(workspace=workspace).exists())

    def test_accounts_without_a_row_count_as_verified(self):
        user = User.objects.create_user("old", "old@example.com", PASSWORD)
        self.assertTrue(services.email_verified(user))


class GoogleProofTests(TestCase):
    def test_google_proof_takes_an_unverified_address_and_the_owner_is_told(self):
        sign_up(Client())
        squatter = User.objects.get(username="maya")
        google_user = services.user_for_google_account("g-1", "Maya@Example.com")
        squatter.refresh_from_db()
        self.assertEqual(google_user.email, "maya@example.com")
        self.assertEqual(squatter.email, "")

        client = Client()
        client.post(
            reverse("accounts:login"), {"username": "maya", "password": PASSWORD}
        )
        response = client.get(reverse("accounts:profile"))
        self.assertContains(response, "no longer has an email address")
        self.assertFalse(services.take_released_notice(squatter))

    def test_a_revoked_link_cannot_sign_in_or_make_a_new_account(self):
        user = User.objects.create_user("owner", "owner@example.com", PASSWORD)
        GoogleIdentity.objects.create(user=user, subject="g-x", email="x@gmail.test")
        services.password_changed(user, had_password=True)
        with self.assertRaises(services.GoogleLinkRevoked):
            services.user_for_google_account("g-x", "x@gmail.test")
        self.assertEqual(User.objects.count(), 1)

    def test_a_first_password_keeps_the_sign_up_link(self):
        user = services.user_for_google_account("g-2", "sam@gmail.test")
        services.password_changed(user, had_password=False)
        services.password_changed(user, had_password=True)
        self.assertIsNone(GoogleIdentity.objects.get(user=user).revoked_at)


class InviteTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("Parent1", "p1@example.com", PASSWORD)
        self.workspace = Workspace.objects.create(
            name="Reyes", primary_admin=self.admin
        )

    def test_the_invite_email_holds_no_family_or_inviter_text(self):
        self.admin.username = "secure-paypal.com"
        self.admin.save()
        self.workspace.name = "Visit https://evil.example now"
        self.workspace.save()
        client = Client()
        client.force_login(self.admin)
        client.post(
            reverse("accounts:manage_members", args=[self.workspace.pk]),
            {"email": "kid@example.com", "role": "kid"},
        )
        message = mail.outbox[0]
        self.assertEqual(
            message.subject, "You have been invited to a family on Syncmily"
        )
        self.assertNotIn("evil", message.body)
        self.assertNotIn("paypal", message.body)

    def test_one_address_gets_one_invite_a_day(self):
        client = Client()
        client.force_login(self.admin)
        membership = services.invite_member(
            workspace=self.workspace,
            invited_by=self.admin,
            email="kid@example.com",
            role="kid",
        )
        url = reverse("accounts:resend_invite", args=[self.workspace.pk, membership.pk])
        client.post(url)
        response = client.post(url, follow=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertContains(response, "a lot of invites")

    def test_stale_invites_go_and_unused_placeholders_with_them(self):
        old = services.invite_member(
            workspace=self.workspace, invited_by=self.admin, email="", role="kid"
        )
        fresh = services.invite_member(
            workspace=self.workspace, invited_by=self.admin, email="", role="kid"
        )
        Membership.objects.filter(pk=old.pk).update(
            invited_at=timezone.now() - timedelta(days=15)
        )
        self.assertEqual(services.purge_stale_invites(), (1, 1))
        self.assertFalse(User.objects.filter(pk=old.user_id).exists())
        self.assertTrue(Membership.objects.filter(pk=fresh.pk).exists())


class UsernameTests(TestCase):
    def test_google_names_follow_the_rules(self):
        self.assertEqual(
            services.available_username("maya+kids@gmail.com"), "maya_kids"
        )
        self.assertEqual(services.available_username("a@gmail.com"), "member")
        self.assertEqual(services.available_username("Parent2@gmail.com"), "Parent2_2")
        self.assertLessEqual(len(services.available_username("x" * 80 + "@g.com")), 30)

    def test_a_rename_is_recorded_and_the_old_name_is_held(self):
        maya = User.objects.create_user("maya", password=PASSWORD)
        other = User.objects.create_user("sam", password=PASSWORD)
        client = Client()
        client.force_login(maya)
        client.post(
            reverse("accounts:profile"), {"action": "username", "username": "maya_r"}
        )
        self.assertTrue(UsernameHistory.objects.filter(user=maya, old="maya").exists())
        client.force_login(other)
        response = client.post(
            reverse("accounts:profile"), {"action": "username", "username": "Maya"}
        )
        self.assertContains(response, "in use recently")

    def test_policy_report_lists_names_outside_the_rules(self):
        User.objects.create_user("José", password=PASSWORD)
        User.objects.create_user("support", password=PASSWORD)
        out = StringIO()
        call_command("report_username_policy", stdout=out)
        self.assertIn("charset", out.getvalue())
        self.assertIn("reserved", out.getvalue())


class SecretsAtRestTests(TestCase):
    def test_seed_is_encrypted_in_the_column_and_codes_still_work(self):
        user = User.objects.create_user("two", password=PASSWORD)
        device = TOTPDevice.objects.create(user=user, name="d", confirmed=True)
        with connection.cursor() as cursor:
            cursor.execute(
                "select key from otp_totp_totpdevice where id=%s", [device.pk]
            )
            raw = cursor.fetchone()[0]
        self.assertTrue(raw.startswith(otp_storage.SEED_PREFIX))
        self.assertLessEqual(len(raw), 80)
        loaded = TOTPDevice.objects.get(pk=device.pk)
        self.assertEqual(loaded.key, device.key)
        totp = TOTP(loaded.bin_key, loaded.step, loaded.t0, loaded.digits)
        self.assertTrue(loaded.verify_token(f"{totp.token():06d}"))

    def test_backup_codes_are_stored_as_hashes(self):
        user = User.objects.create_user("two", password=PASSWORD)
        static = StaticDevice.objects.create(user=user, name="b", confirmed=True)
        StaticToken.objects.create(device=static, token="abcd2345")
        stored = StaticToken.objects.get(device=static).token
        self.assertNotEqual(stored, "abcd2345")
        self.assertIn(stored, otp_storage.code_hashes("ABCD2345"))


@override_settings(OTP_TOTP_THROTTLE_FACTOR=0)
class AdminTwoStepTests(TestCase):
    def test_staff_with_two_step_reach_the_admin_after_the_code(self):
        staff = User.objects.create_user(
            "staffer", password=PASSWORD, is_staff=True, is_superuser=True
        )
        device = TOTPDevice.objects.create(user=staff, name="p", confirmed=True)
        client = Client()
        response = client.post(
            reverse("admin:login"), {"username": "staffer", "password": PASSWORD}
        )
        self.assertRedirects(
            response, reverse("accounts:two_step_verify"), fetch_redirect_response=False
        )
        totp = TOTP(device.bin_key, device.step, device.t0, device.digits)
        client.post(
            reverse("accounts:two_step_verify"), {"code": f"{totp.token():06d}"}
        )
        self.assertEqual(client.get(reverse("admin:index")).status_code, 200)

    def test_a_session_without_the_code_is_kept_out(self):
        staff = User.objects.create_user(
            "staffer", password=PASSWORD, is_staff=True, is_superuser=True
        )
        TOTPDevice.objects.create(user=staff, name="p", confirmed=True)
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get(reverse("admin:index")).status_code, 302)

    def test_staff_without_two_step_sign_in_as_before(self):
        User.objects.create_user(
            "plain", password=PASSWORD, is_staff=True, is_superuser=True
        )
        client = Client()
        client.post(reverse("admin:login"), {"username": "plain", "password": PASSWORD})
        self.assertEqual(client.get(reverse("admin:index")).status_code, 200)


class AccountPauseTests(TestCase):
    def test_twenty_failures_from_many_addresses_pause_password_sign_in(self):
        User.objects.create_user("Parent2", password=PASSWORD)
        for n in range(20):
            Client().post(
                reverse("accounts:login"),
                {"username": "Parent2", "password": "wrong-guess"},
                HTTP_X_FORWARDED_FOR=f"203.0.113.{n}",
            )
        client = Client()
        response = client.post(
            reverse("accounts:login"),
            {"username": "Parent2", "password": PASSWORD},
            HTTP_X_FORWARDED_FOR="198.51.100.1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)
