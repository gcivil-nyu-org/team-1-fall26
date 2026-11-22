"""
Security lane "auth": authentication and account-flow PoCs.

Every test_poc_AUTH* asserts the SECURE behaviour, so it FAILS while the bug exists.
test_cov_* tests record flows that held up (they pass).
Run:
  POSTGRES_DB=syncmily_sec_auth python manage.py test syncmily.tests.security.test_auth_flows
Google, mail and time are mocked. No network, no production system.
"""

import unittest
import re
import smtplib
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import services
from accounts.models import GoogleIdentity, Membership, Workspace

User = get_user_model()
PW = "Sup3r-Secret-test-pw!"
NEW_PW = "An0ther-Secret-pw!-77"
GOOGLE_ON = dict(GOOGLE_CLIENT_ID="test-client", GOOGLE_CLIENT_SECRET="test-secret")
AUTH_URL = "https://accounts.google.com/o/oauth2/auth?mock=1"


def make_user(username, password=PW, **extra):
    extra.setdefault("email", f"{username.lower()}@family.test")
    return User.objects.create_user(username=username, password=password, **extra)


def totp_code(device, step_offset=0):
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time() + step_offset * device.step
    return f"{totp.token():06d}"


def login_post(client, username, password, **extra):
    return client.post(
        reverse("accounts:login"),
        {"username": username, "password": password, **extra.pop("data", {})},
        **extra,
    )


def is_signed_in(client):
    return "_auth_user_id" in client.session


def only_same_site(location, host="testserver"):
    """True when a Location header stays on this site (after browser normalisation)."""
    cleaned = re.sub(r"[\t\r\n]", "", location).replace("\\", "/")
    parts = urlsplit(cleaned)
    if parts.scheme and parts.scheme not in ("http", "https"):
        return False
    return not parts.netloc or parts.netloc == host


def google_claims(sub, email, verified=True):
    return {"sub": sub, "email": email, "email_verified": verified}


def google_sign_in(client, claims, state="STATE-1"):
    """Drive /accounts/google/login/ then the callback with Google mocked."""
    with mock.patch(
        "calendars.google.login_authorization_url",
        return_value=(AUTH_URL, state, "VERIFIER-1"),
    ), mock.patch("calendars.google.verify_login", return_value=claims) as verify:
        client.post(reverse("accounts:google_login"))
        response = client.get(
            reverse("accounts:google_login_callback"), {"state": state, "code": "abc"}
        )
    return response, verify


def google_connect(client, claims, state="STATE-C"):
    """Signed-in user links a Google account from the profile page (mocked)."""
    with mock.patch(
        "calendars.google.login_authorization_url",
        return_value=(AUTH_URL, state, "VERIFIER-C"),
    ), mock.patch("calendars.google.verify_login", return_value=claims):
        client.post(reverse("accounts:google_connect"))
        return client.get(
            reverse("accounts:google_login_callback"), {"state": state, "code": "abc"}
        )


def reset_link(message):
    match = re.search(r"https?://\S+/accounts/reset/\S+/", message.body)
    return urlsplit(match.group(0)).path


def complete_reset(client, link, new_password):
    first = client.get(link)
    target = first["Location"] if first.status_code == 302 else link
    return client.post(
        target, {"new_password1": new_password, "new_password2": new_password}
    )


class AuthBase(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox.clear()

    def sign_up(self, client, username, email, password=PW):
        return client.post(
            reverse("accounts:signup"),
            {
                "username": username,
                "email": email,
                "password1": password,
                "password2": password,
            },
        )

    def family(self, admin_name="Parent1", name="Reyes family"):
        admin = make_user(admin_name)
        workspace = Workspace.objects.create(name=name, primary_admin=admin)
        return admin, workspace

    def invite(self, admin, workspace, email, role="kid"):
        return services.invite_member(
            workspace=workspace, invited_by=admin, email=email, role=role
        )


# ---------------------------------------------------------------------------
# Findings (assert secure behaviour; fail while the bug exists)
# ---------------------------------------------------------------------------
class AuthFindings(AuthBase):
    def test_poc_AUTH01_admin_login_skips_two_step(self):
        """AUTH-01: a staff user with confirmed TOTP signs in at the admin with the
        password only; the resulting session is a full app session (no code asked)."""
        staff = make_user("staffer", is_staff=True, is_superuser=True)
        TOTPDevice.objects.create(user=staff, name="phone", confirmed=True)
        client = Client()
        admin_login = reverse("admin:login")
        response = client.post(
            admin_login,
            {"username": "staffer", "password": PW, "next": reverse("admin:index")},
        )
        self.assertEqual(response.status_code, 302, "admin login with password only")
        # Secure behaviour: no usable session until the second factor is proven.
        self.assertFalse(
            is_signed_in(client), "password alone produced an authenticated session"
        )
        profile = client.get(reverse("accounts:profile"))
        self.assertEqual(profile.status_code, 302, "app page reachable without a code")

    @override_settings(RATELIMIT_ENABLED=True)
    def test_poc_AUTH02_reset_mail_bombing_per_recipient(self):
        """AUTH-02: no per-recipient cap on reset mail; one IP sends 30 an hour and
        many IPs send unlimited to one inbox."""
        make_user("Victim", email="victim@example.test")
        for i in range(40):
            self.client.post(
                reverse("accounts:password_reset"), {"email": "victim@example.test"}
            )
        single_ip = len(mail.outbox)
        cache.clear()
        mail.outbox.clear()
        for i in range(120):
            self.client.post(
                reverse("accounts:password_reset"),
                {"email": "victim@example.test"},
                HTTP_X_FORWARDED_FOR=f"198.51.100.{i % 250}",
            )
        many_ips = len(mail.outbox)
        self.assertLessEqual(
            single_ip, 5, f"{single_ip} reset mails to one inbox from one IP in an hour"
        )
        self.assertLessEqual(
            many_ips, 5, f"{many_ips} reset mails to one inbox from 120 requests"
        )

    def test_poc_AUTH03_reset_response_time_reveals_account(self):
        """AUTH-03: the reset view sends mail inline, so a real account answers slower
        than an unknown address (mail send simulated at 0.3 s)."""
        make_user("Known", email="known@example.test")

        def slow_send(self, *args, **kwargs):
            time.sleep(0.3)
            return 1

        def timed(email):
            start = time.perf_counter()
            response = self.client.post(
                reverse("accounts:password_reset"), {"email": email}
            )
            return time.perf_counter() - start, response.status_code

        with mock.patch("django.core.mail.message.EmailMessage.send", slow_send):
            known, known_status = timed("known@example.test")
            unknown, unknown_status = timed("nobody-here@example.test")
        self.assertEqual(
            known_status, unknown_status
        )  # same redirect, no message oracle
        self.assertLess(
            abs(known - unknown), 0.1, f"known {known:.2f}s vs unknown {unknown:.2f}s"
        )

    def test_cov_reset_mail_failure_does_not_reveal_account(self):
        """Control: Django 5.2 logs and swallows a refused reset mail, so a mail
        failure gives the same answer for real and unknown addresses."""
        make_user("Known", email="known@example.test")
        self.client.raise_request_exception = False
        with mock.patch(
            "django.core.mail.message.EmailMessage.send",
            side_effect=smtplib.SMTPRecipientsRefused(
                {"known@example.test": (554, b"x")}
            ),
        ):
            known = self.client.post(
                reverse("accounts:password_reset"), {"email": "known@example.test"}
            )
            unknown = self.client.post(
                reverse("accounts:password_reset"), {"email": "nobody@example.test"}
            )
        self.assertEqual(
            known.status_code,
            unknown.status_code,
            f"known={known.status_code} unknown={unknown.status_code}",
        )

    def test_poc_AUTH04_signup_reveals_registered_email_and_username(self):
        """AUTH-04: sign-up answers differently for a taken username, an email that
        has an account and an email holding a pending invite."""
        make_user("Taken", email="taken@example.test")
        admin, workspace = self.family()
        self.invite(admin, workspace, "invitee@example.test", role="parent")
        fresh = self.sign_up(Client(), "Fresh1", "fresh@example.test")
        taken_email = self.sign_up(Client(), "Other1", "taken@example.test")
        taken_name = self.sign_up(Client(), "Taken", "other2@example.test")
        invited = self.sign_up(Client(), "Other3", "invitee@example.test")
        self.assertEqual(fresh.status_code, 302)
        for label, response in (
            ("email has an account", taken_email),
            ("username taken", taken_name),
            ("email holds a pending invite", invited),
        ):
            with self.subTest(label):
                self.assertEqual(
                    response.status_code,
                    fresh.status_code,
                    f"{label}: status {response.status_code}, body names the cause",
                )

    def test_poc_AUTH05a_unverified_signup_squats_victim_email_and_blocks_google(self):
        """AUTH-05: sign-up logs in at once with an unverified email. Anyone can claim
        victim@..., which then blocks that person's Google sign-in."""
        attacker = Client()
        response = self.sign_up(attacker, "Squatter", "victim@example.test")
        self.assertEqual(response.status_code, 302)
        victim = Client()
        with override_settings(**GOOGLE_ON):
            google_sign_in(victim, google_claims("g-victim", "victim@example.test"))
        self.assertTrue(
            is_signed_in(victim),
            "the real owner of victim@example.test cannot sign in with Google",
        )

    def test_poc_AUTH05b_prehijack_google_link_survives_victim_password_reset(self):
        """AUTH-05: attacker pre-registers victim's email, links own Google and keeps
        access after the victim recovers the account by emailed reset."""
        with override_settings(**GOOGLE_ON):
            attacker = Client()
            self.sign_up(attacker, "Squatter", "victim@example.test", PW)
            google_connect(attacker, google_claims("g-attacker", "attacker@gmail.test"))
            self.assertTrue(
                GoogleIdentity.objects.filter(subject="g-attacker").exists()
            )
            victim = Client()
            victim.post(
                reverse("accounts:password_reset"), {"email": "victim@example.test"}
            )
            link = reset_link(mail.outbox[-1])
            complete_reset(victim, link, NEW_PW)
            self.assertEqual(
                attacker.get(reverse("accounts:profile")).status_code,
                302,
                "attacker session should be dead after the password changed",
            )
            again = Client()
            google_sign_in(again, google_claims("g-attacker", "attacker@gmail.test"))
        self.assertFalse(
            is_signed_in(again),
            "attacker still signs in via Google after the owner reset the password",
        )

    @override_settings(RATELIMIT_ENABLED=True)
    def test_poc_AUTH06_fresh_unverified_accounts_relay_invite_mail(self):
        """AUTH-06: unverified accounts can mail arbitrary addresses through invites
        (attacker-chosen workspace name in subject and body)."""
        for n in range(3):
            client = Client()
            self.sign_up(client, f"Spam{n}", f"spam{n}@mailinator.test")
            client.post(
                reverse("accounts:create_workspace"),
                {"name": "Your bank locked your card, call 555-0100 now"},
            )
            workspace = Workspace.objects.get(primary_admin__username=f"Spam{n}")
            for i in range(20):
                client.post(
                    reverse("accounts:manage_members", args=[workspace.pk]),
                    {"email": f"target{n}-{i}@victim.test", "role": "kid"},
                )
        sample = mail.outbox[0].subject if mail.outbox else ""
        self.assertEqual(
            len(mail.outbox),
            0,
            f"{len(mail.outbox)} invite mails from 3 brand-new unverified accounts; "
            f"subject={sample!r}",
        )

    def test_poc_AUTH07a_invite_squats_email_and_blocks_google_signin(self):
        """AUTH-07: any parent can invite any address; the inactive invite account
        then blocks that person's Google sign-in and sign-up."""
        admin, workspace = self.family()
        self.invite(admin, workspace, "victim@example.test", role="kid")
        victim = Client()
        with override_settings(**GOOGLE_ON):
            google_sign_in(victim, google_claims("g-victim", "victim@example.test"))
        self.assertTrue(
            is_signed_in(victim),
            "invite to victim@example.test blocked their Google login",
        )

    def test_poc_AUTH07b_invite_creates_duplicate_email_account(self):
        """AUTH-07: inviting an address that already has an active account elsewhere
        creates a second account with the same email (no uniqueness)."""
        make_user("Owner", email="owner@example.test")
        admin, workspace = self.family()
        self.invite(admin, workspace, "owner@example.test", role="parent")
        self.assertEqual(
            User.objects.filter(email__iexact="owner@example.test").count(),
            1,
            "two accounts now share owner@example.test",
        )

    def test_poc_AUTH08_resend_within_one_second_keeps_old_link_valid(self):
        """AUTH-08: invite_stamp is whole seconds, so a resend inside the same second
        does not revoke the earlier link."""
        admin, workspace = self.family()
        membership = self.invite(admin, workspace, "kid@example.test")
        old_token = services.make_invite_token(membership)
        same_second = membership.invited_at.replace(microsecond=999999)
        with mock.patch("django.utils.timezone.now", return_value=same_second):
            services.reissue_invite(membership)
        self.assertIsNone(
            services.get_membership_from_token(old_token),
            "old invite link still accepted after resend",
        )
        response = Client().get(reverse("accounts:accept_invite", args=[old_token]))
        self.assertEqual(response.status_code, 400)

    def test_poc_AUTH09_google_signup_takes_reserved_username(self):
        """AUTH-09: Google sign-up names the account after the email's local part and
        skips the reserved-name check (admin, support, syncmily, parent2...)."""
        names = {}
        with override_settings(**GOOGLE_ON):
            for sub, email in (
                ("g1", "admin@gmail.test"),
                ("g2", "support@gmail.test"),
                ("g3", "parent2@gmail.test"),
            ):
                client = Client()
                google_sign_in(client, google_claims(sub, email))
                names[email] = User.objects.get(google_identity__subject=sub).username
        from accounts.forms import RESERVED_USERNAME

        taken = {e: n for e, n in names.items() if RESERVED_USERNAME.match(n)}
        self.assertEqual(taken, {}, f"reserved usernames handed out: {taken}")

    def test_poc_AUTH10_backup_codes_stored_hashed(self):
        """AUTH-10: single-use backup codes are 40-bit values kept in plaintext in the
        database (ASVS 6.5.2 wants a hash for lookup secrets under 112 bits)."""
        from accounts import two_step

        user = make_user("Twofa")
        codes = two_step._new_backup_codes(user)
        stored = set(StaticToken.objects.values_list("token", flat=True))
        self.assertTrue(
            stored.isdisjoint(codes),
            f"{len(stored & set(codes))} of {len(codes)} backup codes stored in clear",
        )

    # Open decision: session idle length (owner picks the number). Audit REPORT.md (h).
    @unittest.expectedFailure
    def test_poc_AUTH11_session_survives_long_idle(self):
        """AUTH-11: no inactivity timeout; a session idle for 7 days is still valid
        (SESSION_COOKIE_AGE defaults to two weeks, SESSION_SAVE_EVERY_REQUEST is off).
        """
        make_user("Idle")
        client = Client()
        login_post(client, "Idle", PW)
        self.assertEqual(client.get(reverse("accounts:profile")).status_code, 200)
        later = timezone.now() + timedelta(days=7)
        with mock.patch("django.utils.timezone.now", return_value=later):
            response = client.get(reverse("accounts:profile"))
        self.assertEqual(
            response.status_code, 302, "session still valid after 7 days of no use"
        )

    def test_poc_AUTH12_no_account_wide_throttle_across_ips(self):
        """AUTH-12: Axes locks (username, IP) pairs only. 60 wrong passwords for one
        account from 12 addresses cause no account-level friction."""
        make_user("Parent2")
        for ip in range(12):
            for _ in range(5):
                login_post(
                    Client(),
                    "Parent2",
                    "wrong-guess-pw",
                    HTTP_X_FORWARDED_FOR=f"203.0.113.{ip}",
                )
        attacker = Client()
        login_post(attacker, "Parent2", PW, HTTP_X_FORWARDED_FOR="203.0.113.200")
        self.assertFalse(
            is_signed_in(attacker),
            "account accepted its password after 60 failures from 12 IPs",
        )

    def test_poc_AUTH13a_session_alone_can_enrol_attacker_totp(self):
        """AUTH-13: enrolling two-step needs only a live session, no password or code;
        it also signs the real owner out everywhere."""
        make_user("Owner")
        owner_phone = Client()
        login_post(owner_phone, "Owner", PW)
        thief = Client()
        login_post(thief, "Owner", PW)  # stands in for a stolen session cookie
        thief.get(reverse("accounts:two_step_setup"))
        device = TOTPDevice.objects.get(user__username="Owner")
        thief.post(reverse("accounts:two_step_setup"), {"code": totp_code(device)})
        enrolled = TOTPDevice.objects.filter(
            user__username="Owner", confirmed=True
        ).exists()
        self.assertFalse(
            enrolled, "attacker enrolled their own authenticator with no re-auth"
        )

    def test_poc_AUTH13b_session_alone_can_link_attacker_google_and_it_outlives_password_change(
        self,
    ):
        """AUTH-13: linking Google needs only a live session; the link then survives
        the owner changing their password."""
        make_user("Owner")
        with override_settings(**GOOGLE_ON):
            thief = Client()
            login_post(thief, "Owner", PW)
            google_connect(thief, google_claims("g-thief", "thief@gmail.test"))
            owner = Client()
            login_post(owner, "Owner", PW)
            owner.post(
                reverse("accounts:profile"),
                {
                    "action": "password",
                    "old_password": PW,
                    "new_password1": NEW_PW,
                    "new_password2": NEW_PW,
                },
            )
            attacker = Client()
            google_sign_in(attacker, google_claims("g-thief", "thief@gmail.test"))
        self.assertFalse(
            is_signed_in(attacker),
            "Google identity linked from a stolen session still logs in after the "
            "password change",
        )

    def test_poc_AUTH14_limiter_counters_evicted_by_cache_churn(self):
        """AUTH-14: rate-limit counters share the default 300-entry LocMem with route
        caches; 300 other keys reset an exhausted limit (primitive only)."""
        from syncmily import ratelimit

        with override_settings(RATELIMIT_ENABLED=True):
            for _ in range(30):
                ratelimit.hit("password_reset", "ip:198.51.100.9", 30, 3600)
            self.assertFalse(
                ratelimit.hit("password_reset", "ip:198.51.100.9", 30, 3600)
            )
            for i in range(400):  # events/trip.py:360 style keys: trip-google:<coords>
                cache.set(f"trip-google:{i:.4f},0.0000:x", [], 300)
            self.assertFalse(
                ratelimit.hit("password_reset", "ip:198.51.100.9", 30, 3600),
                "limiter forgot the exhausted IP after unrelated cache writes",
            )


# ---------------------------------------------------------------------------
# Controls (flows that held up)
# ---------------------------------------------------------------------------
REDIRECT_PAYLOADS = [
    "//evil.test",
    "///evil.test",
    "/\\evil.test",
    "\\\\evil.test",
    "https:evil.test",
    "https://evil.test",
    "http:/\\evil.test",
    "javascript:alert(1)",
    "\t//evil.test",
    "\n//evil.test",
    "/\t/evil.test",
    " //evil.test",
    "data:text/html,x",
    "//evil.test%2f..",
    "/%09/evil.test",
]


class AuthControls(AuthBase):
    def test_cov_open_redirect_login_next(self):
        make_user("Walker")
        for payload in REDIRECT_PAYLOADS:
            with self.subTest(payload=payload):
                client = Client()
                response = login_post(client, "Walker", PW, data={"next": payload})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    only_same_site(response["Location"]), response["Location"]
                )

    def test_cov_open_redirect_two_step_next(self):
        user = make_user("Walker")
        device = TOTPDevice.objects.create(user=user, name="p", confirmed=True)
        for n, payload in enumerate(REDIRECT_PAYLOADS):
            with self.subTest(payload=payload):
                device.refresh_from_db()
                device.throttle_reset()
                device.last_t = -1
                device.save()
                client = Client()
                login_post(client, "Walker", PW, data={"next": payload})
                response = client.post(
                    reverse("accounts:two_step_verify"),
                    {"code": totp_code(device)},
                )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    only_same_site(response["Location"]), response["Location"]
                )

    def test_cov_open_redirect_set_palette_next(self):
        admin, workspace = self.family()
        for payload in REDIRECT_PAYLOADS:
            with self.subTest(payload=payload):
                client = Client()
                login_post(client, "Parent1", PW)
                response = client.post(
                    reverse("accounts:set_palette", args=[workspace.pk]),
                    {"palette": "garden", "next": payload},
                )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    only_same_site(response["Location"]), response["Location"]
                )

    @override_settings(**GOOGLE_ON)
    def test_cov_open_redirect_calendar_connect_next(self):
        admin, workspace = self.family()
        creds = SimpleNamespace(
            granted_scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            refresh_token="r",
            token="t",
            expiry=None,
        )
        for payload in REDIRECT_PAYLOADS:
            with self.subTest(payload=payload):
                client = Client()
                login_post(client, "Parent1", PW)
                with mock.patch(
                    "calendars.google.authorization_url",
                    return_value=(AUTH_URL, "ST", "VER"),
                ), mock.patch(
                    "calendars.google.exchange_code", return_value=(creds, "p@g.test")
                ), mock.patch(
                    "calendars.views.sync_connection", return_value=0
                ):
                    client.post(
                        reverse("calendars:google_connect", args=[workspace.pk]),
                        {"next": payload},
                    )
                    response = client.get(
                        reverse("calendars:google_callback"),
                        {"state": "ST", "code": "c"},
                    )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    only_same_site(response["Location"]), response["Location"]
                )

    def test_cov_axes_locks_username_ip_pair_after_five(self):
        make_user("Kid1")
        for _ in range(5):
            login_post(
                Client(), "Kid1", "wrong-pw-x", HTTP_X_FORWARDED_FOR="203.0.113.5"
            )
        client = Client()
        response = login_post(client, "Kid1", PW, HTTP_X_FORWARDED_FOR="203.0.113.5")
        self.assertEqual(response.status_code, 429)
        self.assertFalse(is_signed_in(client))
        # fullwidth / NFKC variants of the name hit the same lock
        client = Client()
        login_post(client, "Ｋid1", PW, HTTP_X_FORWARDED_FOR="203.0.113.5")
        self.assertFalse(is_signed_in(client))
        # another address is not locked (the intended trade-off)
        other = Client()
        login_post(other, "Kid1", PW, HTTP_X_FORWARDED_FOR="203.0.113.6")
        self.assertTrue(is_signed_in(other))

    def test_cov_axes_leftmost_xff_is_ignored(self):
        make_user("Kid1")
        for i in range(5):
            login_post(
                Client(),
                "Kid1",
                "wrong-pw-x",
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}, 203.0.113.7",
            )
        client = Client()
        response = login_post(
            client, "Kid1", PW, HTTP_X_FORWARDED_FOR="10.9.9.9, 203.0.113.7"
        )
        self.assertEqual(response.status_code, 429)

    def test_cov_login_error_same_for_unknown_wrong_and_pending(self):
        make_user("Real")
        admin, workspace = self.family()
        pending = self.invite(admin, workspace, "p@example.test")
        bodies = set()
        for name in ("Real", "NoSuchUser", pending.user.username):
            response = login_post(Client(), name, "bad-password-1")
            self.assertEqual(response.status_code, 200)
            bodies.add(
                re.sub(
                    r"csrfmiddlewaretoken\S+|value=\"[^\"]*\"",
                    "",
                    response.content.decode(),
                )
            )
        self.assertEqual(len(bodies), 1)

    def test_cov_logout_invalidates_server_side_session(self):
        make_user("Out")
        client = Client()
        login_post(client, "Out", PW)
        cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        client.post(reverse("accounts:logout"))
        replay = Client()
        replay.cookies[settings.SESSION_COOKIE_NAME] = cookie
        self.assertEqual(replay.get(reverse("accounts:profile")).status_code, 302)

    def test_cov_password_change_ends_other_sessions(self):
        make_user("Pw")
        a, b = Client(), Client()
        login_post(a, "Pw", PW)
        login_post(b, "Pw", PW)
        a.post(
            reverse("accounts:profile"),
            {
                "action": "password",
                "old_password": PW,
                "new_password1": NEW_PW,
                "new_password2": NEW_PW,
            },
        )
        self.assertEqual(a.get(reverse("accounts:profile")).status_code, 200)
        self.assertEqual(b.get(reverse("accounts:profile")).status_code, 302)

    def test_cov_session_key_rotates_on_login(self):
        make_user("Fix")
        client = Client()
        client.get(reverse("accounts:login"))
        client.session.save()
        before = client.cookies.get(settings.SESSION_COOKIE_NAME)
        before = before.value if before else None
        login_post(client, "Fix", PW)
        after = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotEqual(before, after)

    def test_cov_two_step_required_on_password_login_and_backup_code_single_use(self):
        user = make_user("Two")
        TOTPDevice.objects.create(user=user, name="p", confirmed=True)
        static = StaticDevice.objects.create(user=user, name="b", confirmed=True)
        StaticToken.objects.create(device=static, token="abcd2345")
        client = Client()
        login_post(client, "Two", PW)
        self.assertFalse(is_signed_in(client))
        verify = reverse("accounts:two_step_verify")
        client.post(verify, {"code": "abcd2345"})
        self.assertTrue(is_signed_in(client))
        # same backup code again: refused
        static.refresh_from_db()
        static.throttle_reset()
        again = Client()
        login_post(again, "Two", PW)
        again.post(verify, {"code": "abcd2345"})
        self.assertFalse(is_signed_in(again))

    def test_cov_totp_code_single_use_and_pending_expiry_and_attempt_cap(self):
        user = make_user("Two")
        device = TOTPDevice.objects.create(user=user, name="p", confirmed=True)
        code = totp_code(device)
        first = Client()
        login_post(first, "Two", PW)
        first.post(reverse("accounts:two_step_verify"), {"code": code})
        self.assertTrue(is_signed_in(first))
        replay = Client()
        login_post(replay, "Two", PW)
        replay.post(reverse("accounts:two_step_verify"), {"code": code})
        self.assertFalse(is_signed_in(replay))
        # pending login expires after 5 minutes
        late = Client()
        login_post(late, "Two", PW)
        device.refresh_from_db()
        device.throttle_reset()
        with mock.patch("time.time", return_value=time.time() + 400):
            late.post(
                reverse("accounts:two_step_verify"),
                {"code": totp_code(device, step_offset=1)},
            )
        self.assertFalse(is_signed_in(late))

    def test_cov_two_step_not_bypassed_by_google_callback(self):
        user = make_user("Gtwo", email="g@family.test")
        GoogleIdentity.objects.create(user=user, subject="g-two", email="g@family.test")
        TOTPDevice.objects.create(user=user, name="p", confirmed=True)
        client = Client()
        with override_settings(**GOOGLE_ON):
            response, _ = google_sign_in(
                client, google_claims("g-two", "g@family.test")
            )
        self.assertFalse(is_signed_in(client))
        self.assertEqual(response["Location"], reverse("accounts:two_step_verify"))

    def test_cov_invite_token_expiry_tamper_single_use(self):
        admin, workspace = self.family()
        membership = self.invite(admin, workspace, "k@example.test")
        token = services.make_invite_token(membership)
        self.assertEqual(services.get_membership_from_token(token), membership)
        self.assertIsNone(services.get_membership_from_token(token[:-2] + "xx"))
        self.assertIsNone(services.get_membership_from_token("garbage"))
        later = timezone.now() + timedelta(days=8)
        with mock.patch("time.time", return_value=later.timestamp()):
            self.assertIsNone(services.get_membership_from_token(token))
        url = reverse("accounts:accept_invite", args=[token])
        client = Client()
        self.assertEqual(client.get(url).status_code, 200)
        self.assertFalse(is_signed_in(client))  # GET alone never activates
        client.post(url, {"new_password1": PW, "new_password2": PW})
        self.assertTrue(is_signed_in(client))
        second = Client()
        second.post(url, {"new_password1": NEW_PW, "new_password2": NEW_PW})
        self.assertFalse(is_signed_in(second))
        membership.user.refresh_from_db()
        self.assertTrue(membership.user.check_password(PW))

    def test_cov_resend_after_a_second_revokes_old_link(self):
        admin, workspace = self.family()
        membership = self.invite(admin, workspace, "k@example.test")
        old = services.make_invite_token(membership)
        with mock.patch(
            "django.utils.timezone.now",
            return_value=membership.invited_at + timedelta(seconds=3),
        ):
            services.reissue_invite(membership)
        self.assertIsNone(services.get_membership_from_token(old))

    def test_cov_invite_management_is_admin_only_and_membership_ids_scoped(self):
        admin, workspace = self.family()
        other_admin, other_ws = self.family("Parent9", "Other family")
        theirs = self.invite(other_admin, other_ws, "t@example.test")
        client = Client()
        login_post(client, "Parent1", PW)
        resend = reverse("accounts:resend_invite", args=[workspace.pk, theirs.pk])
        remove = reverse("accounts:remove_member", args=[workspace.pk, theirs.pk])
        self.assertEqual(client.post(resend).status_code, 404)
        client.post(remove)
        self.assertTrue(Membership.objects.filter(pk=theirs.pk).exists())
        foreign = reverse("accounts:manage_members", args=[other_ws.pk])
        self.assertEqual(
            client.post(foreign, {"email": "a@b.test", "role": "kid"}).status_code, 403
        )

    @override_settings(**GOOGLE_ON)
    def test_cov_google_state_pkce_replay_unverified_and_no_email_link(self):
        victim = make_user("Existing", email="existing@example.test")
        # state mismatch
        client = Client()
        with mock.patch(
            "calendars.google.login_authorization_url",
            return_value=(AUTH_URL, "S", "V"),
        ), mock.patch("calendars.google.verify_login") as verify:
            client.post(reverse("accounts:google_login"))
            client.get(
                reverse("accounts:google_login_callback"),
                {"state": "WRONG", "code": "c"},
            )
            self.assertEqual(verify.call_count, 0)
            # state is single use
            client.post(reverse("accounts:google_login"))
            verify.return_value = google_claims("g-x", "new@example.test")
            client.get(
                reverse("accounts:google_login_callback"), {"state": "S", "code": "c"}
            )
            client.get(
                reverse("accounts:google_login_callback"), {"state": "S", "code": "c"}
            )
            self.assertEqual(verify.call_count, 1)
            # callback with no prior start (login CSRF)
            fresh = Client()
            fresh.get(
                reverse("accounts:google_login_callback"), {"state": "S", "code": "c"}
            )
            self.assertFalse(is_signed_in(fresh))
            self.assertEqual(verify.call_count, 1)
        # verifier passed through to the exchange
        with mock.patch(
            "calendars.google.login_authorization_url",
            return_value=(AUTH_URL, "S2", "VERIFIER-X"),
        ), mock.patch(
            "calendars.google.verify_login",
            return_value=google_claims("g-y", "y@example.test"),
        ) as v:
            c2 = Client()
            c2.post(reverse("accounts:google_login"))
            c2.get(
                reverse("accounts:google_login_callback"), {"state": "S2", "code": "c"}
            )
            self.assertEqual(v.call_args.args[2], "VERIFIER-X")
        # unverified email refused
        c3 = Client()
        google_sign_in(c3, google_claims("g-z", "z@example.test", verified=False))
        self.assertFalse(is_signed_in(c3))
        # same email as an existing password account is NOT linked or logged in
        c4 = Client()
        google_sign_in(c4, google_claims("g-w", "Existing@Example.test"))
        self.assertFalse(is_signed_in(c4))
        self.assertFalse(GoogleIdentity.objects.filter(user=victim).exists())

    @override_settings(**GOOGLE_ON)
    def test_cov_google_connect_bound_to_initiating_user(self):
        make_user("Alice")
        make_user("Bob")
        client = Client()
        login_post(client, "Alice", PW)
        with mock.patch(
            "calendars.google.login_authorization_url",
            return_value=(AUTH_URL, "S", "V"),
        ), mock.patch(
            "calendars.google.verify_login",
            return_value=google_claims("g-a", "a@g.test"),
        ):
            client.post(reverse("accounts:google_connect"))
            client.logout()
            login_post(client, "Bob", PW)
            client.get(
                reverse("accounts:google_login_callback"), {"state": "S", "code": "c"}
            )
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_cov_reset_same_page_for_known_and_unknown_and_token_single_use(self):
        make_user("Known", email="known@example.test")
        a = self.client.post(
            reverse("accounts:password_reset"), {"email": "known@example.test"}
        )
        b = self.client.post(
            reverse("accounts:password_reset"), {"email": "nobody@example.test"}
        )
        self.assertEqual((a.status_code, a["Location"]), (b.status_code, b["Location"]))
        self.assertEqual(len(mail.outbox), 1)
        link = reset_link(mail.outbox[0])
        complete_reset(Client(), link, NEW_PW)
        self.assertTrue(User.objects.get(username="Known").check_password(NEW_PW))
        reuse = Client()
        complete_reset(reuse, link, "Third-Pw-!-9911")
        self.assertTrue(User.objects.get(username="Known").check_password(NEW_PW))

    def test_cov_reset_link_host_comes_from_allowed_host_only(self):
        make_user("Known", email="known@example.test")
        self.client.raise_request_exception = False
        response = self.client.post(
            reverse("accounts:password_reset"),
            {"email": "known@example.test"},
            HTTP_HOST="evil.test",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)
        self.client.post(
            reverse("accounts:password_reset"),
            {"email": "known@example.test"},
            HTTP_X_FORWARDED_HOST="evil.test",
        )
        self.assertNotIn("evil.test", mail.outbox[-1].body)

    def test_cov_reset_does_not_bypass_two_step(self):
        user = make_user("Two", email="two@example.test")
        TOTPDevice.objects.create(user=user, name="p", confirmed=True)
        self.client.post(
            reverse("accounts:password_reset"), {"email": "two@example.test"}
        )
        client = Client()
        complete_reset(client, reset_link(mail.outbox[0]), NEW_PW)
        self.assertFalse(is_signed_in(client))
        login_post(client, "Two", NEW_PW)
        self.assertFalse(is_signed_in(client))

    def test_cov_two_step_disable_and_new_codes_need_a_code(self):
        user = make_user("Two")
        TOTPDevice.objects.create(user=user, name="p", confirmed=True)
        client = Client()
        client.force_login(user)
        client.post(reverse("accounts:two_step_disable"), {"code": "000000"})
        self.assertTrue(TOTPDevice.objects.filter(user=user).exists())
        client.post(reverse("accounts:two_step_backup_codes"), {"code": "000000"})
        self.assertFalse(StaticDevice.objects.filter(user=user).exists())

    def test_cov_signup_and_login_forms_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        r = csrf_client.post(
            reverse("accounts:login"), {"username": "a", "password": "b"}
        )
        self.assertEqual(r.status_code, 403)
        r = csrf_client.post(reverse("accounts:google_login"))
        self.assertEqual(r.status_code, 403)

    def test_cov_inactive_invited_user_cannot_sign_in_or_reset(self):
        admin, workspace = self.family()
        m = self.invite(admin, workspace, "p@example.test", role="parent")
        self.client.post(
            reverse("accounts:password_reset"), {"email": "p@example.test"}
        )
        self.assertEqual(len(mail.outbox), 0)
        c = Client()
        login_post(c, m.user.username, PW)
        self.assertFalse(is_signed_in(c))


class AuthRaceFindings(TransactionTestCase):
    """AUTH-15: threaded PoC. Also reproduced over HTTP against a private Daphne
    (10 of 10 runs, scratchpad/auth/race3.py)."""

    def test_poc_AUTH15_invite_link_is_not_single_use_under_concurrency(self):
        """AUTH-15: two parallel POSTs of one invite link both pass the is_pending
        check, both set a password and both get a signed-in redirect."""
        import threading

        admin = make_user("Parent1")
        workspace = Workspace.objects.create(name="Race family", primary_admin=admin)
        both_won = 0
        attempts = 12
        for n in range(attempts):
            membership = services.invite_member(
                workspace=workspace,
                invited_by=admin,
                email=f"r{n}@family.test",
                role="kid",
            )
            url = reverse(
                "accounts:accept_invite", args=[services.make_invite_token(membership)]
            )
            barrier = threading.Barrier(2)
            results = [None, None]

            def post(i):
                client = Client()
                try:
                    barrier.wait()
                    pw = f"Racer-{n}-{i}-Secret-pw!"
                    results[i] = client.post(
                        url, {"new_password1": pw, "new_password2": pw}
                    ).status_code
                finally:
                    connection.close()

            threads = [threading.Thread(target=post, args=(i,)) for i in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            if results == [302, 302]:
                both_won += 1
        self.assertEqual(
            both_won,
            0,
            f"both racers consumed the single-use link in {both_won}/{attempts} runs",
        )
