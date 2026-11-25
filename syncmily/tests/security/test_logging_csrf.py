"""
Lane gap4: security event logging, log injection, error handling, CSRF and method sweep.

ASVS 5.0: V16.2.1, V16.3.1, V16.3.2, V16.3.3, V16.3.4, V16.4.1, V16.5.1, V16.5.3,
          V3.5.1, V3.5.3, V15.3.7.  Pitfalls 59 (Logging), 62 (Alerts), 77 (CSRF).

Run:
  cd /Users/simonsavine/syncmily-security-fixes && set -a && . <target.env> && set +a && \
  POSTGRES_DB=syncmily_sec_log python manage.py test \
      syncmily.tests.security.test_logging_csrf --noinput -v 2

Every test_poc_* asserts the SECURE behaviour, so it FAILS while the bug exists.
test_control_* pass today and pin behaviour that must stay secure. Nothing leaves the
process: Google, Groq, MTA and Transitous are mocked or pointed at a closed local port.

Production logging model: see gap4_helpers.py (daphne basicConfig, root INFO, access log
without the user). Records are judged on getMessage(): the text a log line would carry.
"""

import unittest
import re
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode, urlsplit

import requests
from asgiref.sync import async_to_sync
from chat.tests.ws_refusal import connect_and_check
from channels.testing import WebsocketCommunicator
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.db import DatabaseError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.models import Membership
from accounts.services import make_invite_token, reissue_invite
from calendars import fields as calendar_fields
from calendars.models import CalendarConnection
from events import trip as trip_mod
from events.models import Event
from syncmily import ratelimit
from syncmily.asgi import application

from .gap4_helpers import (
    IP,
    PW,
    build_world,
    capture,
    describe,
    judge,
    redact,
    register_secret,
    snapshot,
)

User = get_user_model()


def totp_code(device):
    t = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    t.time = time.time()
    return f"{t.token():06d}"


def login_post(client, username, password, **extra):
    return client.post(
        reverse("accounts:login"),
        {"username": username, "password": password},
        REMOTE_ADDR=IP,
        **extra,
    )


@override_settings(RATELIMIT_ENABLED=True)
class LogBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        build_world(cls)

    def setUp(self):
        cache.clear()

    def client_for(self, user, **kw):
        client = Client(REMOTE_ADDR=IP, raise_request_exception=False, **kw)
        client.force_login(user)
        return client

    def assertLogged(self, handler, *, what, who, where=(), label=""):
        ok, report = judge(handler, what=what, who=who, where=where)
        self.assertTrue(
            ok,
            f"\n{label}: no log record with who+what+where+outcome. {report}\n"
            f"records emitted during the event:\n{describe(handler.records)}",
        )


# ===========================================================================
# 1. Security event logging (V16.2.1, V16.3.1-V16.3.4)
# ===========================================================================


class SecurityEventLoggingTests(LogBase):
    # --- authentication -----------------------------------------------------

    def test_poc_LOG_001_failed_login_known_user_is_logged(self):
        """GAP4-LOG-001 / V16.3.1: a failed password for a real account is logged with
        username, client IP, route and outcome."""
        with capture() as cap:
            r = login_post(Client(), self.parent.username, "not-the-password-1")
        self.assertEqual(r.status_code, 200)
        self.assertLogged(
            cap,
            what=r"login|sign.?in|authenticat|password",
            who=[self.parent.username, IP],
            where=[IP, "/accounts/login/"],
            label="failed login (known user)",
        )

    def test_poc_LOG_002_failed_login_unknown_user_is_logged(self):
        """GAP4-LOG-002 / V16.3.1: a failed login for a name that does not exist is
        logged with the attempted name and the client IP."""
        with capture() as cap:
            login_post(Client(), "no_such_user_gap4", "not-the-password-1")
        self.assertLogged(
            cap,
            what=r"login|sign.?in|authenticat",
            who=["no_such_user_gap4", IP],
            where=[IP, "/accounts/login/"],
            label="failed login (unknown user)",
        )

    def test_poc_LOG_003_successful_login_is_logged(self):
        """GAP4-LOG-003 / V16.3.1: a successful sign-in is logged (who, from where)."""
        client = Client()
        with capture() as cap:
            r = login_post(client, self.parent.username, PW)
        self.assertEqual(r.status_code, 302)
        self.assertLogged(
            cap,
            what=r"login|sign.?in|authenticat|logged",
            who=[self.parent.username, str(self.parent.pk)],
            where=[IP, "/accounts/login/"],
            label="successful login",
        )

    def test_poc_LOG_004_axes_lockout_is_logged(self):
        """GAP4-LOG-004 / V16.3.1, pitfall 62: the 5th wrong password locks the
        username+IP pair; the lock must be logged with who, IP and outcome (the
        CloudWatch lockout alarm matches this text)."""
        make = Client()
        with capture() as cap:
            for _ in range(5):
                login_post(Client(), self.parent.username, "wrong-guess-gap4")
            r = login_post(make, self.parent.username, PW)
        self.assertEqual(r.status_code, 429, "lockout did not trigger; test is invalid")
        self.assertLogged(
            cap,
            what=r"lock",
            who=[self.parent.username],
            where=[IP, "/accounts/login/"],
            label="axes lockout",
        )

    def _two_step_user(self, name):
        user = User.objects.create_user(username=name, password=PW)
        device = TOTPDevice.objects.create(user=user, name="app", confirmed=True)
        return user, device

    def test_poc_LOG_005_wrong_totp_code_is_logged(self):
        """GAP4-LOG-005 / V16.3.1: a wrong second-step code is logged (who, IP,
        'two-step'/'code', failed)."""
        user, _ = self._two_step_user("LogTotpUser")
        client = Client()
        with capture() as cap:
            r = login_post(client, user.username, PW)
            self.assertEqual(r.status_code, 302)
            client.post(
                reverse("accounts:two_step_verify"), {"code": "000000"}, REMOTE_ADDR=IP
            )
        self.assertLogged(
            cap,
            what=r"two.?step|totp|otp|code|verification|2fa|mfa",
            who=[user.username, str(user.pk)],
            where=[IP, "/accounts/login/verify/"],
            label="wrong TOTP code",
        )

    def test_poc_LOG_006_backup_code_use_is_logged(self):
        """GAP4-LOG-006 / V16.3.1: spending a single-use backup code is a notable
        credential event; it must be logged."""
        user, _ = self._two_step_user("LogBackupUser")
        static = StaticDevice.objects.create(user=user, name="Backup", confirmed=True)
        StaticToken.objects.create(device=static, token="bkup4321")
        register_secret("bkup4321")
        client = Client()
        with capture() as cap:
            login_post(client, user.username, PW)
            r = client.post(
                reverse("accounts:two_step_verify"),
                {"code": "bkup4321"},
                REMOTE_ADDR=IP,
            )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(StaticToken.objects.filter(device=static).count(), 0)
        self.assertLogged(
            cap,
            what=r"backup",
            who=[user.username, str(user.pk)],
            where=[IP, "/accounts/login/verify/"],
            label="backup code used",
        )

    def test_poc_LOG_007_password_reset_request_is_logged(self):
        """GAP4-LOG-007 / V16.3.1: a password-reset request is logged (target
        account or address, IP, outcome)."""
        with capture() as cap:
            Client().post(
                reverse("accounts:password_reset"),
                {"email": self.parent.email},
                REMOTE_ADDR=IP,
            )
        self.assertLogged(
            cap,
            what=r"reset",
            who=[self.parent.email, self.parent.username, str(self.parent.pk), IP],
            where=[IP, "/accounts/password-reset/"],
            label="password reset request",
        )

    def test_poc_LOG_008_password_reset_confirm_is_logged(self):
        """GAP4-LOG-008 / V16.3.1: completing a reset (the password changes without the
        old one) is logged."""
        uid = urlsafe_base64_encode(force_bytes(self.parent.pk))
        token = default_token_generator.make_token(self.parent)
        register_secret(token)
        client = Client()
        with capture() as cap:
            r = client.get(
                reverse("accounts:password_reset_confirm", args=[uid, token]),
                REMOTE_ADDR=IP,
            )
            self.assertEqual(r.status_code, 302)
            r = client.post(
                r["Location"],
                {
                    "new_password1": "Brand-New-Gap4-Pw-77",
                    "new_password2": "Brand-New-Gap4-Pw-77",
                },
                REMOTE_ADDR=IP,
            )
        self.assertEqual(r.status_code, 302, "reset did not complete; test is invalid")
        self.assertLogged(
            cap,
            what=r"reset|password",
            who=[self.parent.username, str(self.parent.pk), IP],
            where=[IP, "/accounts/reset/"],
            label="password reset completed",
        )

    def test_poc_LOG_009_invite_accept_is_logged(self):
        """GAP4-LOG-009 / V16.3.1: accepting an invite creates a session without a
        password check, axes or two-step; it must be logged."""
        token = make_invite_token(self.pending)
        register_secret(token)
        client = Client()
        with capture() as cap:
            r = client.post(
                reverse("accounts:accept_invite", args=[token]),
                {
                    "new_password1": "Invite-Gap4-Pw-77x",
                    "new_password2": "Invite-Gap4-Pw-77x",
                },
                REMOTE_ADDR=IP,
            )
        self.assertEqual(r.status_code, 302, "invite was not accepted; test is invalid")
        self.assertLogged(
            cap,
            what=r"invit",
            who=[
                self.pending.user.username,
                str(self.pending.user.pk),
                str(self.pending.pk),
            ],
            where=[IP, "/accounts/invite/"],
            label="invite accepted",
        )

    def test_poc_LOG_017_signup_is_logged(self):
        """GAP4-LOG-017 / V16.3.1: a new account (signed in at once, no verification)
        is logged."""
        with capture() as cap:
            r = Client().post(
                reverse("accounts:signup"),
                {
                    "username": "LogNewcomer",
                    "email": "newcomer@gap4.example",
                    "password1": "Newcomer-Gap4-77!",
                    "password2": "Newcomer-Gap4-77!",
                },
                REMOTE_ADDR=IP,
            )
        self.assertEqual(r.status_code, 302, "signup failed; test is invalid")
        self.assertLogged(
            cap,
            what=r"sign.?up|regist|account|creat",
            who=["LogNewcomer"],
            where=[IP, "/accounts/signup/"],
            label="signup",
        )

    def test_poc_LOG_018_password_change_is_logged(self):
        """GAP4-LOG-018 / V16.3.1: a signed-in password change is logged."""
        client = self.client_for(self.parent)
        with capture() as cap:
            r = client.post(
                reverse("accounts:profile"),
                {
                    "action": "password",
                    "old_password": PW,
                    "new_password1": "Changed-Gap4-Pw-77",
                    "new_password2": "Changed-Gap4-Pw-77",
                },
            )
        self.assertEqual(r.status_code, 302, "password change failed; test is invalid")
        self.assertLogged(
            cap,
            what=r"password",
            who=[self.parent.username, str(self.parent.pk)],
            where=[IP, "/accounts/profile/"],
            label="password changed",
        )

    def test_poc_LOG_020_two_step_disable_is_logged(self):
        """GAP4-LOG-020 / V16.3.1: turning two-step off (needs a valid code) is a
        security-control change and must be logged."""
        user, device = self._two_step_user("LogDisableUser")
        client = self.client_for(user)
        with capture() as cap:
            client.post(
                reverse("accounts:two_step_disable"), {"code": totp_code(device)}
            )
        self.assertFalse(TOTPDevice.objects.filter(user=user).exists())
        self.assertLogged(
            cap,
            what=r"two.?step|totp|2fa|mfa",
            who=[user.username, str(user.pk)],
            where=[IP, "/accounts/two-step/disable/"],
            label="two-step disabled",
        )

    # --- authorization ------------------------------------------------------

    def test_poc_LOG_010_cross_household_event_404_is_logged(self):
        """GAP4-LOG-010 / V16.3.2: another family's event answers 404. A probe of event
        ids is an attack pattern; the refusal must be logged with who and the route."""
        client = self.client_for(self.parent_b)
        with capture() as cap:
            r = client.get(reverse("events:detail", args=[self.event.pk]))
        self.assertEqual(r.status_code, 404)
        self.assertLogged(
            cap,
            what=r"not found|denied|forbidden|refus|unauthori|404",
            who=[self.parent_b.username, str(self.parent_b.pk)],
            where=[f"/events/{self.event.pk}/"],
            label="cross-household event 404",
        )

    def test_poc_LOG_011_cross_household_membership_403_is_logged(self):
        """GAP4-LOG-011 / V16.3.2: another admin opening this family's member page
        gets 403; log who asked for which workspace."""
        client = self.client_for(self.admin_b)
        with capture() as cap:
            r = client.get(reverse("accounts:manage_members", args=[self.ws.pk]))
        self.assertEqual(r.status_code, 403)
        self.assertLogged(
            cap,
            what=r"forbidden|denied|permission|refus|403",
            who=[self.admin_b.username, str(self.admin_b.pk)],
            where=[str(self.ws.pk)],
            label="cross-household members 403",
        )

    def test_poc_LOG_012_kid_on_parent_only_route_is_logged(self):
        """GAP4-LOG-012 / V16.3.2: a kid opening parent-only routes is refused; log it."""
        client = self.client_for(self.kid)
        routes = [
            reverse("events:workspace_create", args=[self.ws.pk]),
            reverse("accounts:workspace_detail", args=[self.ws.pk]),
        ]
        for url in routes:
            with self.subTest(url=url), capture() as cap:
                r = client.get(url)
                self.assertIn(r.status_code, (403, 404))
                self.assertLogged(
                    cap,
                    what=r"forbidden|denied|permission|refus|not found|40[34]",
                    who=[self.kid.username, str(self.kid.pk)],
                    where=[url],
                    label=f"kid on parent-only route {url}",
                )

    # --- anti-automation ----------------------------------------------------

    def _prime(self, scope, window, limit, idents):
        for ident in idents:
            ratelimit.set_count(scope, ident, window, limit)

    def test_poc_LOG_013_every_429_is_logged_with_who(self):
        """GAP4-LOG-013 / V16.3.3: each rate limiter that answers 429 logs who hit it,
        which limiter and from where."""
        loner = self.client_for(self.loner)
        admin = self.client_for(self.admin)
        cases = [
            (
                "login",
                "/accounts/login/",
                None,
                lambda: self._prime("login", 600, 150, [f"ip:{IP}"]),
                lambda: Client().post(
                    reverse("accounts:login"),
                    {"username": "x", "password": "y"},
                    REMOTE_ADDR=IP,
                ),
                ["x", IP],
            ),
            (
                "signup",
                "/accounts/signup/",
                None,
                lambda: self._prime("signup", 3600, 50, [f"ip:{IP}"]),
                lambda: Client().post(reverse("accounts:signup"), {}, REMOTE_ADDR=IP),
                [IP],
            ),
            (
                "password_reset",
                "/accounts/password-reset/",
                None,
                lambda: self._prime("password_reset", 3600, 30, [f"ip:{IP}"]),
                lambda: Client().post(
                    reverse("accounts:password_reset"),
                    {"email": "a@b.test"},
                    REMOTE_ADDR=IP,
                ),
                [IP],
            ),
            (
                "two_step_verify",
                "/accounts/login/verify/",
                None,
                lambda: self._prime("two_step_verify", 600, 60, [f"ip:{IP}"]),
                lambda: Client().post(
                    reverse("accounts:two_step_verify"), {"code": "1"}, REMOTE_ADDR=IP
                ),
                [IP],
            ),
            (
                "create_workspace",
                "/accounts/workspaces/create/",
                self.loner,
                lambda: self._prime(
                    "create_workspace", 3600, 5, [f"user:{self.loner.pk}"]
                ),
                lambda: loner.post(reverse("accounts:create_workspace"), {"name": "x"}),
                [self.loner.username, str(self.loner.pk), IP],
            ),
            (
                "checklist_create",
                "/checklists/workspaces/",
                self.admin,
                lambda: self._prime(
                    "checklist_create", 60, 60, [f"ip:{IP}", f"user:{self.admin.pk}"]
                ),
                lambda: admin.post(
                    reverse("checklists:create", args=[self.ws.pk]), {"title": "x"}
                ),
                [self.admin.username, str(self.admin.pk), IP],
            ),
            (
                "checklist_item_add",
                "/checklists/",
                self.admin,
                lambda: self._prime(
                    "checklist_item_add", 60, 60, [f"ip:{IP}", f"user:{self.admin.pk}"]
                ),
                lambda: admin.post(
                    reverse("checklists:item_add", args=[self.checklist.pk]),
                    {"description": "x"},
                ),
                [self.admin.username, str(self.admin.pk), IP],
            ),
        ]
        for scope, path, _user, prime, send, who in cases:
            with self.subTest(limiter=scope):
                cache.clear()
                prime()
                with capture() as cap:
                    r = send()
                self.assertEqual(
                    r.status_code, 429, f"{scope}: limiter did not answer 429"
                )
                self.assertLogged(
                    cap,
                    what=r"too many|rate|limit|throttl|429",
                    who=who,
                    where=[path],
                    label=f"429 from limiter {scope}",
                )

    # --- request-level security failures ------------------------------------

    def test_poc_LOG_014_csrf_failure_is_logged_with_who(self):
        """GAP4-LOG-014 / V16.3.3: a forged cross-site POST (CSRF failure) is logged
        with the signed-in user (or IP) and the route."""
        client = Client(enforce_csrf_checks=True, REMOTE_ADDR=IP)
        client.force_login(self.parent)
        with capture() as cap:
            r = client.post(
                reverse("accounts:profile"), {"action": "username", "username": "x"}
            )
        self.assertEqual(r.status_code, 403)
        self.assertLogged(
            cap,
            what=r"csrf",
            who=[self.parent.username, str(self.parent.pk), IP],
            where=["/accounts/profile/"],
            label="CSRF failure",
        )

    def test_poc_LOG_015_disallowed_host_is_logged_with_client(self):
        """GAP4-LOG-015 / V16.3.3: a request with a foreign Host header is rejected;
        the log must say which client sent it."""
        with capture() as cap:
            r = Client(raise_request_exception=False).get(
                "/accounts/login/", HTTP_HOST="evil.gap4.example", REMOTE_ADDR=IP
            )
        self.assertEqual(r.status_code, 400)
        self.assertLogged(
            cap,
            what=r"host",
            who=[IP],
            where=["evil.gap4.example"],
            label="DisallowedHost",
        )

    def test_poc_LOG_019_unhandled_error_is_logged_with_who(self):
        """GAP4-LOG-019 / V16.3.4: an unhandled exception (HTTP 500) is logged with the
        user and the route (not only a traceback)."""
        client = self.client_for(self.parent)
        with capture() as cap, mock.patch(
            "accounts.views._week_context", side_effect=RuntimeError("boom-gap4")
        ):
            r = client.get(reverse("accounts:dashboard"))
        self.assertEqual(r.status_code, 500)
        self.assertLogged(
            cap,
            what=r"error|exception|500|boom",
            who=[self.parent.username, str(self.parent.pk)],
            where=["/accounts/dashboard/"],
            label="unhandled 500",
        )

    # --- no secrets in any record --------------------------------------------

    def test_poc_LOG_021_no_secret_in_any_record(self):
        """GAP4-LOG-021 / V16.2.5 / V14.2.1, pitfall 59: no password, TOTP code, backup
        code, invite or reset token, session id or CSRF token appears in any record
        (message or traceback), across the whole set of flows."""
        secrets = {}
        wrong_pw = "Wr0ng-LEAKCHECK-Pw!"
        secrets["wrong password"] = wrong_pw
        user, device = self._two_step_user("LogLeakUser")
        wrong_code = "654321" if totp_code(device) != "654321" else "654322"
        secrets["wrong totp"] = wrong_code
        secrets["valid totp"] = totp_code(device)
        static = StaticDevice.objects.create(user=user, name="Backup", confirmed=True)
        StaticToken.objects.create(device=static, token="leak4321")
        secrets["backup code"] = "leak4321"
        stale = make_invite_token(self.pending)
        reissue_invite(Membership.objects.get(pk=self.pending.pk))
        secrets["stale invite token"] = stale
        second_user = User.objects.create_user(
            username="LogPendingKid2", password=None, is_active=False
        )
        second = Membership.objects.create(
            workspace=self.ws,
            user=second_user,
            role=Membership.Role.KID,
            invited_by=self.admin,
            accepted_at=None,
        )
        live = make_invite_token(second)
        secrets["invite token (accepted)"] = live
        uid = urlsafe_base64_encode(force_bytes(self.parent.pk))
        reset = default_token_generator.make_token(self.parent)
        secrets["reset token"] = reset
        for value in secrets.values():
            register_secret(value)

        with capture() as cap:
            client = Client(REMOTE_ADDR=IP, enforce_csrf_checks=False)
            login_post(client, self.parent.username, wrong_pw)
            login_post(client, user.username, PW)
            client.post(
                reverse("accounts:two_step_verify"),
                {"code": wrong_code},
                REMOTE_ADDR=IP,
            )
            client.post(
                reverse("accounts:two_step_verify"),
                {"code": "leak4321"},
                REMOTE_ADDR=IP,
            )
            sid = client.cookies.get("sessionid")
            if sid:
                register_secret(sid.value)
                secrets["session id"] = sid.value
            anon = Client(REMOTE_ADDR=IP)
            anon.get(reverse("accounts:accept_invite", args=[stale]))  # 400, dead token
            anon.post(  # a real accept: the token is live until this request succeeds
                reverse("accounts:accept_invite", args=[live]),
                {
                    "new_password1": "Leak-Check-Pw-77xy",
                    "new_password2": "Leak-Check-Pw-77xy",
                },
            )
            anon.delete(
                reverse("accounts:password_reset_confirm", args=[uid, reset])
            )  # 405
            csrf_client = Client(
                enforce_csrf_checks=True, REMOTE_ADDR=IP, raise_request_exception=False
            )
            csrf_client.get(reverse("accounts:login"))
            csrf = csrf_client.cookies.get("csrftoken")
            if csrf:
                register_secret(csrf.value)
                secrets["csrf token"] = csrf.value
            csrf_client.post(
                reverse("accounts:login"), {"username": "a", "password": wrong_pw}
            )

        leaks = []
        for name, value in secrets.items():
            for line in cap.lines():
                if value in line:
                    leaks.append(f"{name} appears in: {redact(line)[:160]}")
        self.assertFalse(
            leaks,
            "secrets found in log records:\n  "
            + "\n  ".join(leaks)
            + f"\nall records:\n{describe(cap.records)}",
        )

    # --- controls: logging that works today -----------------------------------

    def test_control_member_removal_is_logged(self):
        """Control: remove_member logs ids of the person, the family and the actor."""
        client = self.client_for(self.admin)
        with capture() as cap:
            client.post(
                reverse("accounts:remove_member", args=[self.ws.pk, self.pending.pk])
            )
        self.assertTrue(
            any("removed from workspace" in m for m in cap.messages()),
            describe(cap.records),
        )


# ===========================================================================
# 2. Sink sanity: records reach a timestamped handler; WebSocket 4403
# ===========================================================================


class SinkTests(TestCase):
    def test_control_daphne_format_carries_time_level_message(self):
        """Control: with daphne's default log_fmt a line has a timestamp and level
        ('when' of V16.2.1). The access log has no user, so 'who' must come from the
        app's own records."""
        from .gap4_helpers import DAPHNE_FMT

        self.assertIn("%(asctime)", DAPHNE_FMT)
        self.assertIn("%(levelname)", DAPHNE_FMT)


def _make_socket(workspace, cookie, origin=b"http://localhost:8000"):
    headers = [(b"host", b"localhost"), (b"origin", origin)]
    if cookie:
        headers.append((b"cookie", f"sessionid={cookie}".encode()))
    return WebsocketCommunicator(
        application, f"/ws/chat/{workspace.id}/", headers=headers
    )


class WebSocketLoggingTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        build_world(self)

    def test_poc_LOG_016_websocket_4403_is_logged(self):
        """GAP4-LOG-016 / V16.3.2: a member of family B joining family A's chat socket is
        closed with 4403; the refusal must be logged (who, which workspace, outcome).
        daphne's access log only says 'WSREJECT <path>' with an address, never the user.
        """
        client = Client()
        client.force_login(self.parent_b)
        cookie = client.cookies["sessionid"].value
        register_secret(cookie)

        async def run():
            communicator = _make_socket(self.ws, cookie)
            connected, code = await connect_and_check(communicator)
            return connected, code

        with capture() as cap:
            connected, code = async_to_sync(run)()
        self.assertFalse(connected, "foreign member was allowed in; test is invalid")
        ok, report = judge(
            cap,
            what=r"websocket|chat|socket|4403|refus|denied|forbidden",
            who=[self.parent_b.username, str(self.parent_b.pk)],
            where=[
                str(self.ws.pk),
                str(self.ws.pk.hex) if hasattr(self.ws.pk, "hex") else "",
            ],
        )
        self.assertTrue(
            ok,
            f"WebSocket 4403 close (code={code}) left no record with who/what/where/outcome. "
            f"{report}\nrecords:\n{describe(cap.records)}",
        )


# ===========================================================================
# 3. Log injection (V16.4.1)
# ===========================================================================

FORGED = (
    "\r\n2026-11-18 20:00:00,000 INFO     AXES: successful login by admin from 127.0.0.1"
    "\x1b[2J\x1b[31m"
)


def has_control(text):
    return re.search(r"[\r\n\x1b\x00-\x08\x0b\x0c\x0e-\x1f]", text) is not None


class LogInjectionTests(LogBase):
    def test_poc_LOG_022_username_cannot_forge_log_line(self):
        """GAP4-LOG-022 / V16.4.1: the login username is attacker-controlled and ends up
        in axes' records. A CR/LF + ANSI value must not reach a record as a raw line break.
        """
        name = "victim" + FORGED
        with capture() as cap:
            for _ in range(
                6
            ):  # 5 failures + the locked attempt exercise every axes log path
                Client().post(
                    reverse("accounts:login"),
                    {"username": name, "password": "x-gap4-wrong"},
                    REMOTE_ADDR=IP,
                )
        injected = [m for m in cap.messages() if has_control(m)]
        self.assertFalse(
            injected,
            "raw CR/LF/ESC reached a log message:\n  "
            + "\n  ".join(repr(redact(m))[:240] for m in injected)
            + f"\nall records:\n{describe(cap.records)}",
        )

    @override_settings(AXES_SENSITIVE_PARAMETERS=[])
    def test_poc_LOG_022b_username_cannot_forge_line_once_axes_masking_is_off(self):
        """GAP4-LOG-022b / V16.4.1 (latent): today axes masks username and IP in every record
        (AXES_SENSITIVE_PARAMETERS default), which is also why LOG-001/003/004 lack 'who'.
        The obvious fix, turning the masking off, makes the attacker-chosen username part
        of the record. This test turns it off and asserts the record still cannot be
        forged; it must pass before the LOG-001 fix ships."""
        name = "victim" + FORGED
        with capture() as cap:
            for _ in range(6):
                Client().post(
                    reverse("accounts:login"),
                    {"username": name, "password": "x-gap4-wrong"},
                    REMOTE_ADDR=IP,
                )
        injected = [m for m in cap.messages() if has_control(m)]
        self.assertFalse(
            injected,
            "with axes masking off, raw CR/LF/ESC reached a log message:\n  "
            + "\n  ".join(repr(m)[:260] for m in injected),
        )

    def test_control_request_path_cannot_forge_log_line(self):
        """Control (Django >= 5.2.3 escapes the path in django.request): a CR/LF + ANSI
        path produces one escaped line."""
        with capture() as cap:
            Client(raise_request_exception=False).get(
                "/%0d%0a2026-11-18%2020:00:00,000%20INFO%20forged%1b[31m",
                REMOTE_ADDR=IP,
            )
        bad = [m for m in cap.messages() if has_control(m)]
        self.assertFalse(bad, [repr(m) for m in bad])

    def test_poc_LOG_023_mta_line_cannot_forge_log_line(self):
        """GAP4-LOG-023 / V16.4.1: events/trip.py:627 logs segment["line"] (upstream
        Google/Transitous data) with %s. A line name carrying CR/LF + ANSI must not forge a
        second log line."""
        now = timezone.now()
        segment = {
            "kind": "ride",
            "vehicle": "SUBWAY",
            "route_id": "A",
            "depart": now + timedelta(hours=1),
            "arrive": now + timedelta(hours=2),
            "from_point": (40.6, -73.9),
            "to_point": (40.7, -74.0),
            "board_ids": set(),
            "alight_ids": set(),
            "station_ids": set(),
            "line": "A" + FORGED,
        }
        plan = {
            "segments": [segment],
            "leave_by": now,
            "arrive": now + timedelta(hours=2),
        }
        with capture() as cap, mock.patch.object(
            trip_mod.mta, "feed_for_line", return_value="feed"
        ), mock.patch.object(
            trip_mod.mta, "next_departures", side_effect=ValueError("feed down")
        ):
            trip_mod.check_live(plan, now, trip_mod.MtaStatus())
        self.assertTrue(
            any("MTA live feed unavailable" in m for m in cap.messages()),
            "the logger.warning call was not reached; test is invalid:\n"
            + describe(cap.records),
        )
        bad = [m for m in cap.messages() if has_control(m)]
        self.assertFalse(bad, "forged line reached the log: " + repr(bad)[:300])

    @override_settings(GOOGLE_MAPS_API_KEY="")
    def test_poc_LOG_025_trip_failure_log_does_not_carry_home_coordinates(self):
        """GAP4-LOG-025 / V14.2.1, pitfall 59: events/trip.py:729 logs the requests
        exception with exc_info=True; its text contains the full Transitous URL, i.e. the
        family's start point and the event coordinates. A real connection error (closed local
        port, no external call) shows what is written."""
        cache.clear()
        origin = SimpleNamespace(
            latitude=40.689201, longitude=-73.944201, address="742 Evergreen Terrace"
        )
        event = Event.objects.get(pk=self.event.pk)
        event.transport_mode = Event.TransportMode.TRANSIT
        event.save(update_fields=["transport_mode"])
        with capture() as cap, mock.patch.object(
            trip_mod, "TRANSITOUS_URL", "http://127.0.0.1:9/plan"
        ):
            result = trip_mod.build_trip(event, self.admin, origin=origin)
        self.assertEqual(
            result["status"], "error", "failure path not reached; test is invalid"
        )
        blob = "\n".join(cap.lines())
        self.assertTrue(
            any("Trip request failed" in m for m in cap.messages()),
            describe(cap.records),
        )
        self.assertNotIn("40.689201", blob, "home latitude is in the log")
        self.assertNotIn("-73.944201", blob, "home longitude is in the log")


# ===========================================================================
# 4. Error handling (V16.5.1, V16.5.3)
# ===========================================================================

LEAK_MARKERS = (
    "Traceback",
    'File "',
    ".py",
    "SECRET_KEY",
    "settings.",
    "SELECT ",
    "relation ",
    "INSERT ",
    "psycopg",
    "django.db",
    "boom-secret-gap4",
    "DJANGO_",
)


class ErrorHandlingTests(LogBase):
    def _assert_generic(self, response, label):
        body = response.content.decode(errors="replace")
        self.assertEqual(response.status_code, 500, f"{label}: expected 500")
        leaked = [m for m in LEAK_MARKERS if m in body]
        self.assertFalse(leaked, f"{label}: 500 body leaks {leaked}")
        self.assertIn("Something went wrong", body, f"{label}: not the generic page")

    def test_control_500_html_body_is_generic(self):
        """Control (V16.5.1): a RuntimeError with a secret marker and a DatabaseError
        carrying SQL both render the generic 500 page on an HTML route."""
        client = self.client_for(self.parent)
        errors = (
            RuntimeError("boom-secret-gap4 SECRET_KEY=abc"),
            DatabaseError(
                'relation "accounts_user" does not exist LINE 1: SELECT * FROM x'
            ),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__), mock.patch(
                "accounts.views._week_context", side_effect=error
            ):
                self._assert_generic(
                    client.get(reverse("accounts:dashboard")), "dashboard"
                )

    def test_poc_GAP4_500_json_route_returns_generic_json_not_html(self):
        """GAP4-ERR-001 / V16.5.1: a JSON route (chat message_page, fetched by chat_home.js)
        that raises returns the full HTML error page; assert it returns no internals (control
        part) and a JSON-safe body for an Accept: application/json client."""
        client = self.client_for(self.parent)
        with mock.patch(
            "chat.views.get_message_page", side_effect=RuntimeError("boom-secret-gap4")
        ):
            r = client.get(
                reverse("chat:messages", args=[self.ws.pk]),
                HTTP_ACCEPT="application/json",
            )
        body = r.content.decode(errors="replace")
        self.assertEqual(r.status_code, 500)
        self.assertFalse(
            [m for m in LEAK_MARKERS if m in body], "internals in 500 body"
        )
        self.assertIn(
            "json",
            r.headers.get("Content-Type", ""),
            "an Accept: application/json request got a non-JSON 500 (client JS cannot parse it)",
        )

    def test_control_403_and_404_bodies_do_not_echo_the_exception_message(self):
        client = self.client_for(self.kid)
        r = client.get(reverse("accounts:workspace_detail", args=[self.ws.pk]))
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(
            "You do not have permission to access this workspace", r.content.decode()
        )
        r = client.get("/no/such/page/")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("Using the URLconf", r.content.decode())

    # --- failed decrypt (calendars/fields.py) ---------------------------------

    def _connect_google(self, user, key):
        calendar_fields._fernet_for.cache_clear()
        with override_settings(FIELD_ENCRYPTION_KEYS=[key]):
            CalendarConnection.objects.create(
                user=user,
                workspace=self.ws if user.pk == self.admin.pk else None,
                provider=CalendarConnection.Provider.GOOGLE,
                account_email="g@gap4.example",
                access_token="ya29.gap4-access",
                refresh_token="1//gap4-refresh",
            )
        calendar_fields._fernet_for.cache_clear()

    def test_poc_GAP4_decrypt_failure_must_not_break_pages(self):
        """GAP4-ERR-002 / V16.5.3: calendars/fields.py raises ImproperlyConfigured when a
        stored token cannot be decrypted (key rotated or mistyped). google_calendar_context
        runs from the shell context processor on every page, so every page for every user
        with a connection then answers 500. A failed optional feature (the Google
        account menu) must degrade, not take the app down."""
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        self._connect_google(self.parent, old)
        self._connect_google(self.admin, old)
        calendar_fields._fernet_for.cache_clear()
        statuses = {}
        try:
            with override_settings(FIELD_ENCRYPTION_KEYS=[new]), capture() as cap:
                for who, user in (("parent", self.parent), ("admin", self.admin)):
                    client = self.client_for(user)
                    urls = [
                        reverse("accounts:dashboard"),
                        reverse("events:list"),
                        reverse("chat:workspace_chat", args=[self.ws.pk]),
                        reverse("checklists:list", args=[self.ws.pk]),
                        reverse("accounts:profile"),
                    ]
                    if who == "admin":
                        urls.append(
                            reverse("accounts:workspace_detail", args=[self.ws.pk])
                        )
                    for url in urls:
                        statuses[(who, url)] = client.get(url).status_code
                kid_client = self.client_for(self.kid)
                statuses[("kid", "dashboard")] = kid_client.get(
                    reverse("accounts:dashboard")
                ).status_code
        finally:
            calendar_fields._fernet_for.cache_clear()
        self.assertTrue(
            any("can't be decrypted" in r.gap4_text for r in cap.records),
            "the 500s were not caused by the decrypt failure; test is invalid",
        )
        broken = {k: v for k, v in statuses.items() if k[0] != "kid" and v >= 500}
        self.assertEqual(
            statuses[("kid", "dashboard")], 200, "kid without a connection must be fine"
        )
        self.assertFalse(
            broken,
            f"{len(broken)} of {len(statuses) - 1} pages answer 5xx after a decrypt failure: "
            f"{sorted(broken.items())[:6]}",
        )

    def test_poc_GAP4_user_can_still_disconnect_after_decrypt_failure(self):
        """GAP4-ERR-003 / V16.5.3: with an undecryptable token the user cannot even
        disconnect Google (the view loads the row, so it 500s), so the fault cannot be
        repaired from the UI."""
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        self._connect_google(self.parent, old)
        calendar_fields._fernet_for.cache_clear()
        try:
            with override_settings(FIELD_ENCRYPTION_KEYS=[new]), mock.patch(
                "calendars.google.revoke"
            ):
                r = self.client_for(self.parent).post(
                    reverse("calendars:google_disconnect")
                )
                gone = not CalendarConnection.objects.filter(user=self.parent).exists()
        finally:
            calendar_fields._fernet_for.cache_clear()
        self.assertLess(r.status_code, 500, f"disconnect answered {r.status_code}")
        self.assertTrue(gone, "the unusable connection row is still there")


# ===========================================================================
# 5. CSRF and method sweep (V3.5.1, V3.5.3, V15.3.7)
# ===========================================================================


def route_table(w):
    """name -> (url, post_data). Every named route of the five apps, plus home."""
    ws, ev, cl, it, mem = w.ws.pk, w.event.pk, w.checklist.pk, w.item.pk, w.pending.pk
    uid = urlsafe_base64_encode(force_bytes(w.parent.pk))
    default_token_generator.make_token(w.parent)
    when = {
        "start_date": "2030-01-01",
        "start_time": "10:00",
        "end_date": "2030-01-01",
        "end_time": "11:00",
        "timezone": "America/New_York",
    }
    r = reverse
    return {
        "home": ("/", {}),
        # events
        "events:list": (r("events:list"), {}),
        "events:choose_workspace": (
            r("events:choose_workspace"),
            {"workspace": str(ws)},
        ),
        "events:create": (r("events:create"), {"action": "save", "title": "t"}),
        "events:workspace_list": (r("events:workspace_list", args=[ws]), {}),
        "events:workspace_create": (
            r("events:workspace_create", args=[ws]),
            {"action": "save", "title": "csrf-sweep", **when},
        ),
        "events:scan_ocr": (r("events:scan_ocr", args=[ws]), {}),
        "events:member_colors": (
            r("events:member_colors", args=[ws]),
            {f"color-{w.kid.pk}": "#112233"},
        ),
        "events:timezones": (r("events:timezones"), {}),
        "events:place_suggestions": (r("events:place_suggestions"), {"q": "park"}),
        "events:place_details": (r("events:place_details"), {"id": "ChIJabcdefghij"}),
        "events:weekends": (r("events:weekends"), {}),
        "events:detail": (
            r("events:detail", args=[ev]),
            {"dropoff_driver": str(w.parent.pk), "pickup_driver": str(w.parent.pk)},
        ),
        "events:edit": (
            r("events:edit", args=[ev]),
            {"action": "save", "title": "csrf-sweep", **when},
        ),
        "events:delete": (r("events:delete", args=[ev]), {"action": "delete"}),
        "events:set_transport_mode": (
            r("events:set_transport_mode", args=[ev]),
            {"transport_mode": "walk"},
        ),
        "events:trip": (
            r("events:trip", args=[ev]),
            {"mode": "here", "latitude": "40.1", "longitude": "-73.9"},
        ),
        "events:change_time": (
            r("events:change_time", args=[ev]),
            {"start_date": "2030-01-01"},
        ),
        # chat
        "chat:home": (r("chat:home"), {}),
        "chat:messages": (r("chat:messages", args=[ws]), {}),
        "chat:workspace_chat": (r("chat:workspace_chat", args=[ws]), {}),
        # accounts
        "accounts:create_workspace": (
            r("accounts:create_workspace"),
            {"name": "csrf-sweep"},
        ),
        "accounts:workspace_detail": (r("accounts:workspace_detail", args=[ws]), {}),
        "accounts:manage_members": (
            r("accounts:manage_members", args=[ws]),
            {"role": "kid", "email": ""},
        ),
        "accounts:resend_invite": (r("accounts:resend_invite", args=[ws, mem]), {}),
        "accounts:remove_member": (r("accounts:remove_member", args=[ws, mem]), {}),
        "accounts:dashboard": (r("accounts:dashboard"), {}),
        "accounts:day_seen": (r("accounts:day_seen"), {}),
        "accounts:profile": (
            r("accounts:profile"),
            {"action": "username", "username": "csrf-sweep"},
        ),
        "accounts:accept_invite": (
            r("accounts:accept_invite", args=[make_invite_token(w.pending)]),
            {
                "new_password1": "Sweep-Gap4-Pw-77x",
                "new_password2": "Sweep-Gap4-Pw-77x",
            },
        ),
        "accounts:signup": (
            r("accounts:signup"),
            {
                "username": "csrfsweep",
                "email": "s@gap4.example",
                "password1": "Sweep-Gap4-Pw-77x",
                "password2": "Sweep-Gap4-Pw-77x",
            },
        ),
        # Added with the email-ownership routes (hardening spec 3.4, package p04).
        "accounts:verify_email": (
            r("accounts:verify_email", args=["not-a-valid-link"]),
            {},
        ),
        "accounts:resend_verification": (r("accounts:resend_verification"), {}),
        "accounts:google_login": (r("accounts:google_login"), {}),
        "accounts:google_connect": (r("accounts:google_connect"), {}),
        "accounts:google_disconnect": (r("accounts:google_disconnect"), {}),
        "accounts:google_login_callback": (r("accounts:google_login_callback"), {}),
        "accounts:set_palette": (
            r("accounts:set_palette", args=[ws]),
            {"palette": "ocean"},
        ),
        "accounts:family_home": (
            r("accounts:family_home", args=[ws]),
            {"action": "clear"},
        ),
        "accounts:set_my_palette": (
            r("accounts:set_my_palette", args=[ws]),
            {"palette": "ocean"},
        ),
        "accounts:login": (
            r("accounts:login"),
            {"username": w.parent.username, "password": PW},
        ),
        "accounts:two_step_verify": (r("accounts:two_step_verify"), {"code": "000000"}),
        "accounts:two_step_setup": (r("accounts:two_step_setup"), {"code": "000000"}),
        "accounts:two_step_disable": (
            r("accounts:two_step_disable"),
            {"code": "000000"},
        ),
        "accounts:two_step_backup_codes": (
            r("accounts:two_step_backup_codes"),
            {"code": "000000"},
        ),
        "accounts:password_reset": (
            r("accounts:password_reset"),
            {"email": w.parent.email},
        ),
        "accounts:password_reset_done": (r("accounts:password_reset_done"), {}),
        "accounts:password_reset_confirm": (
            r("accounts:password_reset_confirm", args=[uid, "set-password"]),
            {
                "new_password1": "Sweep-Gap4-Pw-77x",
                "new_password2": "Sweep-Gap4-Pw-77x",
            },
        ),
        "accounts:password_reset_complete": (r("accounts:password_reset_complete"), {}),
        "accounts:logout": (r("accounts:logout"), {}),
        # calendars
        "calendars:google_connect": (r("calendars:google_connect", args=[ws]), {}),
        "calendars:google_callback": (r("calendars:google_callback"), {}),
        "calendars:google_sync": (r("calendars:google_sync"), {}),
        "calendars:google_auto_sync": (
            r("calendars:google_auto_sync"),
            {"auto_sync": "off"},
        ),
        "calendars:google_disconnect": (r("calendars:google_disconnect"), {}),
        # checklists
        "checklists:list": (r("checklists:list", args=[ws]), {}),
        "checklists:create": (
            r("checklists:create", args=[ws]),
            {"title": "csrf-sweep"},
        ),
        "checklists:detail": (r("checklists:detail", args=[cl]), {}),
        "checklists:edit": (r("checklists:edit", args=[cl]), {"title": "csrf-sweep"}),
        "checklists:delete": (r("checklists:delete", args=[cl]), {}),
        "checklists:item_add": (
            r("checklists:item_add", args=[cl]),
            {"description": "csrf-sweep"},
        ),
        "checklists:item_edit": (
            r("checklists:item_edit", args=[it]),
            {"description": "csrf-sweep"},
        ),
        "checklists:item_delete": (r("checklists:item_delete", args=[it]), {}),
        "checklists:item_toggle": (
            r("checklists:item_toggle", args=[it]),
            {"completed": "1"},
        ),
        # admin (default Django admin, path from settings.ADMIN_PATH)
        "admin:login": (r("admin:login"), {"username": "x", "password": "y"}),
        "admin:logout": (r("admin:logout"), {}),
        "admin:password_change": (r("admin:password_change"), {}),
    }


def resolver_route_names():
    """Full 'ns:name' of every named pattern in the five apps + home (from the URLconf)."""
    names = {"home"}

    def walk(patterns, ns):
        for p in patterns:
            if hasattr(p, "url_patterns"):
                sub = p.namespace
                if sub in ("admin",):
                    continue
                walk(p.url_patterns, f"{ns}{sub}:" if sub else ns)
            elif getattr(p, "name", None):
                names.add(f"{ns}{p.name}")

    walk(get_resolver().url_patterns, "")
    return names


@override_settings(RATELIMIT_ENABLED=False)
class CsrfAndMethodSweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        build_world(cls)

    def setUp(self):
        cache.clear()
        self.client = Client(
            enforce_csrf_checks=True, raise_request_exception=False, REMOTE_ADDR=IP
        )
        self.client.force_login(self.parent)  # a signed-in parent, session cookie only

    def _no_network(self):
        calls = []

        def refuse(session, method, url, **kw):
            calls.append((method, url))
            raise requests.ConnectionError("gap4: network disabled")

        return calls, mock.patch("requests.sessions.Session.request", refuse)

    def test_control_sweep_table_covers_every_named_route(self):
        """The sweep table matches the URLconf, so a new route cannot slip past it."""
        table = set(route_table(self).keys())
        urlconf = resolver_route_names()
        self.assertFalse(
            urlconf - table, f"routes missing from the sweep: {sorted(urlconf - table)}"
        )
        # recon-result.json lists 64 routes including nginx/static rows; the 60 named app
        # routes + home + admin entries here are what Django can serve.
        self.assertGreaterEqual(len(table), 60)

    def test_control_valid_token_is_accepted_and_foreign_origin_is_not(self):
        """Control: the harness really enforces CSRF. A POST with the cookie token passes;
        the same token with a cross-site Origin is refused."""
        self.client.get(reverse("accounts:profile"))
        token = self.client.cookies["csrftoken"].value
        url = reverse("accounts:profile")
        ok = self.client.post(
            url, {"action": "username", "username": "tokenok"}, HTTP_X_CSRFTOKEN=token
        )
        self.assertNotEqual(ok.status_code, 403)
        evil = self.client.post(
            url,
            {"action": "username", "username": "evil"},
            HTTP_X_CSRFTOKEN=token,
            HTTP_ORIGIN="https://evil.gap4.example",
        )
        self.assertEqual(evil.status_code, 403)

    def test_control_GAP4_csrf_tokenless_post_refused_on_every_route(self):
        """GAP4-CSRF-001 / V3.5.1, pitfall 77: a POST without a CSRF token returns 403 and
        changes nothing on every route (the authz lane used a client that skips CSRF).
        """
        calls, patch = self._no_network()
        bad = []
        with patch:
            for name, (url, data) in route_table(self).items():
                before = snapshot()
                response = self.client.post(url, data)
                after = snapshot()
                if response.status_code != 403:
                    bad.append(f"{name} POST {url} -> {response.status_code}")
                if before != after:
                    bad.append(f"{name} POST {url} CHANGED STATE")
        self.assertFalse(calls, f"outbound calls during a tokenless sweep: {calls}")
        self.assertFalse(bad, "\n  " + "\n  ".join(bad))

    def test_control_healthz_ignores_csrf_but_is_stateless(self):
        before = snapshot()
        r = self.client.post("/healthz/", {"x": "1"})
        self.assertEqual((r.status_code, r.content), (200, b"ok"))
        self.assertEqual(before, snapshot())

    def test_poc_GAP4_get_and_head_never_change_state(self):
        """GAP4-CSRF-002 / V3.5.3, V15.3.7: GET and HEAD carrying the POST parameters in the
        query string change nothing on any route (checked for routes that accept any
        method: chat, workspace_detail, dashboard, checklist_list, ...)."""
        calls, patch = self._no_network()
        bad, skipped, served = [], [], 0
        with patch:
            for name, (url, data) in route_table(self).items():
                if name in (
                    "accounts:logout",
                    "admin:logout",
                    "accounts:two_step_setup",
                ):
                    continue  # logout asserted below; two_step_setup has its own PoC
                query = urlencode(data)
                full = f"{url}?{query}" if query else url
                for verb in ("get", "head"):
                    before = snapshot()
                    response = getattr(self.client, verb)(full)
                    after = snapshot()
                    if response.status_code == 405:
                        skipped.append(name)
                        break
                    served += 1
                    if before != after:
                        bad.append(
                            f"{name} {verb.upper()} {full} CHANGED STATE ({response.status_code})"
                        )
                    if response.status_code >= 500:
                        bad.append(
                            f"{name} {verb.upper()} {full} -> {response.status_code}"
                        )
        self.assertGreater(served, 40, "sweep served too few routes to mean anything")
        self.assertFalse(bad, "\n  " + "\n  ".join(bad))

    # Open: same as GAP4_010 (two-step setup POST-only). Audit REPORT.md (h).
    @unittest.expectedFailure
    def test_poc_GAP4_two_step_setup_get_creates_a_device_row(self):
        """GAP4-CSRF-004 / V3.5.3, V15.3.7: GET /accounts/two-step/setup/ writes a row (an
        unconfirmed TOTPDevice with a fresh secret) for the signed-in user. A cross-site
        link or image can trigger it. It is low impact (the device does nothing until a
        code confirms it) but a safe method must not write."""
        before = snapshot()
        self.client.get(reverse("accounts:two_step_setup"))
        self.assertEqual(
            before, snapshot(), "GET changed the database (TOTPDevice created)"
        )

    def test_control_GAP4_logout_get_does_not_sign_out(self):
        """GAP4-CSRF-003 / V3.5.3: GET and HEAD on /accounts/logout/ must not end the
        session (a link or image could sign people out)."""
        for verb in ("get", "head"):
            getattr(self.client, verb)(reverse("accounts:logout"))
            self.assertIn(
                "_auth_user_id",
                self.client.session,
                f"{verb.upper()} logged the user out",
            )

    # --- duplicate parameters: the value checked is the value used -------------

    def _login_location(self, body):
        c = Client(REMOTE_ADDR=IP)
        r = c.post(
            reverse("accounts:login"),
            body,
            content_type="application/x-www-form-urlencoded",
        )
        return r

    def _same_site(self, location):
        cleaned = re.sub(r"[\t\r\n]", "", location).replace("\\", "/")
        parts = urlsplit(cleaned)
        return not parts.netloc and parts.scheme in ("", "http", "https")

    def test_control_GAP4_duplicate_next_cannot_split_check_and_use(self):
        """GAP4-DUP-001 / V15.3.7: `next` sent twice (body, query, either order) never makes
        the redirect leave the site, on login, transport mode, weekends and the palette
        routes."""
        evil = "https://evil.gap4.example/steal"
        good = "/accounts/dashboard/"
        bad = []
        for first, second in ((good, evil), (evil, good)):
            body = urlencode(
                [
                    ("username", self.parent.username),
                    ("password", PW),
                    ("next", first),
                    ("next", second),
                ]
            )
            r = self._login_location(body)
            if not self._same_site(r.get("Location", "")):
                bad.append(
                    f"login body next={first!r},{second!r} -> {r.get('Location')}"
                )
            # body vs query-string
            c = Client(REMOTE_ADDR=IP)
            r = c.post(
                reverse("accounts:login") + "?" + urlencode({"next": first}),
                {"username": self.parent.username, "password": PW, "next": second},
            )
            if not self._same_site(r.get("Location", "")):
                bad.append(
                    f"login query next={first!r} + body next={second!r} -> {r.get('Location')}"
                )
            for name in (
                "events:set_transport_mode",
                "events:weekends",
                "accounts:set_palette",
                "accounts:set_my_palette",
            ):
                url, data = route_table(self)[name]
                self.client.get(reverse("accounts:profile"))
                self.client.cookies["csrftoken"].value
                pairs = list(data.items()) + [("next", first), ("next", second)]
                actor = Client(
                    enforce_csrf_checks=True,
                    REMOTE_ADDR=IP,
                    raise_request_exception=False,
                )
                actor.force_login(
                    self.kid if name == "accounts:set_my_palette" else self.parent
                )
                actor.get(reverse("accounts:profile"))
                tok = actor.cookies["csrftoken"].value
                r = actor.post(
                    url,
                    urlencode(pairs),
                    content_type="application/x-www-form-urlencoded",
                    HTTP_X_CSRFTOKEN=tok,
                )
                if r.status_code in (301, 302) and not self._same_site(r["Location"]):
                    bad.append(f"{name} next={first!r},{second!r} -> {r['Location']}")
        self.assertFalse(bad, "\n  " + "\n  ".join(bad))

    def _post_form(self, user, url, pairs):
        actor = Client(
            enforce_csrf_checks=True, REMOTE_ADDR=IP, raise_request_exception=False
        )
        actor.force_login(user)
        actor.get(reverse("accounts:profile"))
        return actor.post(
            url,
            urlencode(pairs),
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CSRFTOKEN=actor.cookies["csrftoken"].value,
        )

    def test_control_duplicate_action_uses_one_consistent_value(self):
        """Control (V15.3.7): action=delete&action=keep / keep&delete on the delete route
        behave exactly like the single LAST value, so the confirmation check and the
        deletion read the same value. (Last-wins is Django's QueryDict.get; noted in the report.)
        """
        url = reverse("events:delete", args=[self.event.pk])
        r = self._post_form(self.admin, url, [("action", "delete"), ("action", "keep")])
        self.assertTrue(
            Event.objects.filter(pk=self.event.pk).exists(),
            f"deleted on delete,keep ({r.status_code})",
        )
        r = self._post_form(self.admin, url, [("action", "keep"), ("action", "delete")])
        deleted = not Event.objects.filter(pk=self.event.pk).exists()
        self.assertTrue(
            deleted, "keep,delete did not behave like a single 'delete' (split)"
        )

    def test_control_GAP4_duplicate_driver_values_cannot_assign_a_foreign_user(self):
        """GAP4-DUP-002 / V15.3.7: two dropoff_driver values (own parent + a user of another
        family, either order) must never persist a driver outside event_drivers(workspace).
        """
        from events.access import event_drivers

        url = reverse("events:detail", args=[self.event.pk])
        for first, second in (
            (self.parent.pk, self.parent_b.pk),
            (self.parent_b.pk, self.parent.pk),
        ):
            with self.subTest(first=first, second=second):
                self._post_form(
                    self.admin,
                    url,
                    [
                        ("dropoff_driver", first),
                        ("dropoff_driver", second),
                        ("pickup_driver", self.admin.pk),
                    ],
                )
                event = Event.objects.get(pk=self.event.pk)
                allowed = set(event_drivers(self.ws).values_list("pk", flat=True)) | {
                    None
                }
                self.assertIn(event.dropoff_driver_id, allowed)
                self.assertNotEqual(event.dropoff_driver_id, self.parent_b.pk)

    def test_control_GAP4_duplicate_workspace_param_cannot_open_a_foreign_family(self):
        """GAP4-DUP-003 / V15.3.7: choose-workspace with workspace=<own>&workspace=<foreign>
        (either order) never redirects into the foreign family's route."""
        url = reverse("events:choose_workspace")
        for first, second in ((self.ws.pk, self.ws_b.pk), (self.ws_b.pk, self.ws.pk)):
            with self.subTest(first=first, second=second):
                r = self.client.get(
                    url + "?" + urlencode([("workspace", first), ("workspace", second)])
                )
                if r.status_code in (301, 302):
                    self.assertNotIn(str(self.ws_b.pk), r["Location"])

    def test_control_duplicate_completed_param_uses_last_value(self):
        """Control: item_toggle reads one `completed` value; duplicates behave like the last."""
        url = reverse("checklists:item_toggle", args=[self.item.pk])
        self._post_form(self.admin, url, [("completed", "0"), ("completed", "1")])
        self.item.refresh_from_db()
        self.assertTrue(self.item.is_completed)
