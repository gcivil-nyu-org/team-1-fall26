"""
Lane gap4 findings: one PoC per finding id (GAP4-001 ... GAP4-011).

Run:
  cd /Users/simonsavine/syncmily-security-fixes && set -a && . <target.env> && set +a && \
  POSTGRES_DB=syncmily_sec_gap4 python manage.py test syncmily.tests.security.test_gap4 --noinput -v 2

Every test_poc_GAP4_* asserts the SECURE behaviour, so it FAILS while the bug exists.
test_control_GAP4_* pass today. The broad sweep behind these (every event, every route, 43
tests) is in test_logging_csrf.py; shared helpers are in gap4_helpers.py (daphne's
production logging model: root INFO handler, access log without the user).
"""

import unittest
import re
import time
from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from asgiref.sync import async_to_sync
from chat.tests.ws_refusal import connect_and_check
from channels.testing import WebsocketCommunicator
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.models import Membership
from accounts.services import make_invite_token
from calendars import fields as calendar_fields
from calendars.models import CalendarConnection
from events import trip as trip_mod
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
FORGED = (
    "\r\n2026-11-18 20:00:00,000 INFO     AXES: successful login by admin from 127.0.0.1"
    "\x1b[2J\x1b[31m"
)


def totp_code(device):
    t = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    t.time = time.time()
    return f"{t.token():06d}"


def login(client, name, password):
    return client.post(
        reverse("accounts:login"),
        {"username": name, "password": password},
        REMOTE_ADDR=IP,
    )


@override_settings(RATELIMIT_ENABLED=True)
class Gap4Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        build_world(cls)

    def setUp(self):
        cache.clear()

    def signed_in(self, user, **kw):
        client = Client(REMOTE_ADDR=IP, raise_request_exception=False, **kw)
        client.force_login(user)
        return client

    def two_step_user(self, name):
        user = User.objects.create_user(username=name, password=PW)
        device = TOTPDevice.objects.create(user=user, name="app", confirmed=True)
        return user, device

    def check_events(self, events):
        """events: list of (label, run(), what_regex, who_list, where_list). One subTest each."""
        for label, run, what, who, where in events:
            with self.subTest(event=label):
                cache.clear()
                with capture() as cap:
                    run()
                ok, report = judge(cap, what=what, who=who, where=where)
                self.assertTrue(
                    ok, f"{label}: {report}\nrecords emitted:\n{describe(cap.records)}"
                )


class SecurityEventLogging(Gap4Base):
    def test_poc_GAP4_001_security_events_leave_no_log_record(self):
        """GAP4-001 / ASVS V16.3.1, V16.3.2 / pitfall 59, 62: these security events emit NO
        log record at all (accounts/two_step.py and views.py have no logger calls)."""
        totp_user, device = self.two_step_user("G4TotpUser")
        backup_user, _ = self.two_step_user("G4BackupUser")
        static = StaticDevice.objects.create(user=backup_user, name="B", confirmed=True)
        StaticToken.objects.create(device=static, token="g4bkup99")
        register_secret("g4bkup99")
        disable_user, disable_device = self.two_step_user("G4DisableUser")
        uid = urlsafe_base64_encode(force_bytes(self.parent.pk))
        token = default_token_generator.make_token(self.parent)
        register_secret(token)

        def wrong_totp():
            c = Client()
            login(c, totp_user.username, PW)
            c.post(
                reverse("accounts:two_step_verify"), {"code": "000000"}, REMOTE_ADDR=IP
            )

        def backup():
            c = Client()
            login(c, backup_user.username, PW)
            c.post(
                reverse("accounts:two_step_verify"),
                {"code": "g4bkup99"},
                REMOTE_ADDR=IP,
            )

        def reset_request():
            Client().post(
                reverse("accounts:password_reset"),
                {"email": self.parent.email},
                REMOTE_ADDR=IP,
            )

        def reset_confirm():
            c = Client()
            r = c.get(
                reverse("accounts:password_reset_confirm", args=[uid, token]),
                REMOTE_ADDR=IP,
            )
            c.post(
                r["Location"],
                {
                    "new_password1": "Reset-G4-Pw-7788",
                    "new_password2": "Reset-G4-Pw-7788",
                },
                REMOTE_ADDR=IP,
            )

        def pw_change():
            self.signed_in(self.admin).post(
                reverse("accounts:profile"),
                {
                    "action": "password",
                    "old_password": PW,
                    "new_password1": "Changed-G4-Pw-7788",
                    "new_password2": "Changed-G4-Pw-7788",
                },
            )

        def disable():
            self.signed_in(disable_user).post(
                reverse("accounts:two_step_disable"),
                {"code": totp_code(disable_device)},
            )

        self.check_events(
            [
                (
                    "wrong TOTP code",
                    wrong_totp,
                    r"two.?step|totp|code|otp|2fa|mfa",
                    [totp_user.username, str(totp_user.pk)],
                    [IP, "/accounts/login/verify/"],
                ),
                (
                    "backup code spent",
                    backup,
                    r"backup",
                    [backup_user.username, str(backup_user.pk)],
                    [IP, "/accounts/login/verify/"],
                ),
                (
                    "password reset requested",
                    reset_request,
                    r"reset",
                    [self.parent.email, self.parent.username, str(self.parent.pk), IP],
                    [IP],
                ),
                (
                    "password reset completed",
                    reset_confirm,
                    r"reset|password",
                    [self.parent.username, str(self.parent.pk), IP],
                    [IP],
                ),
                (
                    "password changed",
                    pw_change,
                    r"password",
                    [self.admin.username, str(self.admin.pk)],
                    [IP, "/accounts/profile/"],
                ),
                (
                    "two-step turned off",
                    disable,
                    r"two.?step|totp|2fa|mfa",
                    [disable_user.username, str(disable_user.pk)],
                    [IP, "/accounts/two-step/disable/"],
                ),
            ]
        )

    def test_poc_GAP4_002_axes_records_mask_username_and_ip(self):
        """GAP4-002 / V16.2.1, V16.3.1 / pitfall 59, 62: sign-in, failure and lockout ARE
        logged (by django-axes) but axes masks username and ip_address by default
        (AXES_SENSITIVE_PARAMETERS, axes/conf.py:150), so no record says who or from where.
        Sign-ups, invite accepts and two-step logins ride the same masked record."""
        pending_token = make_invite_token(self.pending)
        register_secret(pending_token)

        def lockout():
            for _ in range(5):
                login(Client(), self.parent.username, "wrong-guess-g4")
            login(Client(), self.parent.username, PW)

        self.check_events(
            [
                (
                    "failed login, real user",
                    lambda: login(Client(), self.parent.username, "wrong-1-g4"),
                    r"login|sign.?in",
                    [self.parent.username, IP],
                    [IP, "/accounts/login/"],
                ),
                (
                    "failed login, unknown user",
                    lambda: login(Client(), "nobody_g4", "wrong-1-g4"),
                    r"login|sign.?in",
                    ["nobody_g4", IP],
                    [IP, "/accounts/login/"],
                ),
                (
                    "successful login",
                    lambda: login(Client(), self.parent.username, PW),
                    r"login|sign.?in",
                    [self.parent.username, str(self.parent.pk)],
                    [IP, "/accounts/login/"],
                ),
                (
                    "lockout",
                    lockout,
                    r"lock",
                    [self.parent.username],
                    [IP, "/accounts/login/"],
                ),
                (
                    "sign-up",
                    lambda: Client().post(
                        reverse("accounts:signup"),
                        {
                            "username": "G4Newcomer",
                            "email": "g4n@gap4.example",
                            "password1": "Newcomer-G4-7788!",
                            "password2": "Newcomer-G4-7788!",
                        },
                        REMOTE_ADDR=IP,
                    ),
                    r"sign.?up|account|regist|login",
                    ["G4Newcomer"],
                    [IP, "/accounts/signup/"],
                ),
                (
                    "invite accepted",
                    lambda: Client().post(
                        reverse("accounts:accept_invite", args=[pending_token]),
                        {
                            "new_password1": "Invite-G4-Pw-7788",
                            "new_password2": "Invite-G4-Pw-7788",
                        },
                        REMOTE_ADDR=IP,
                    ),
                    r"invit",
                    [self.pending.user.username, str(self.pending.user.pk)],
                    [IP],
                ),
            ]
        )

    def test_poc_GAP4_003_refusals_and_failures_are_logged_without_who(self):
        """GAP4-003 / V16.3.2, V16.3.3, V16.3.4 / pitfall 59: authorization refusals, 429s,
        CSRF failures, bad Host headers and 500s reach the log only as Django's one-line
        'Forbidden (...): /path' with no user and no client address."""
        kid, parent_b, admin_b = self.kid, self.parent_b, self.admin_b

        def event_404():
            self.signed_in(parent_b).get(reverse("events:detail", args=[self.event.pk]))

        def members_403():
            self.signed_in(admin_b).get(
                reverse("accounts:manage_members", args=[self.ws.pk])
            )

        def kid_route():
            self.signed_in(kid).get(
                reverse("events:workspace_create", args=[self.ws.pk])
            )

        def limiter():
            ratelimit.set_count("login", f"ip:{IP}", 600, 150)
            login(Client(), "x", "y")

        def csrf():
            c = Client(enforce_csrf_checks=True, REMOTE_ADDR=IP)
            c.force_login(self.parent)
            c.post(reverse("accounts:profile"), {"action": "username", "username": "x"})

        def host():
            Client().get(
                "/accounts/login/", HTTP_HOST="evil.g4.example", REMOTE_ADDR=IP
            )

        def boom():
            with mock.patch(
                "accounts.views._week_context", side_effect=RuntimeError("g4")
            ):
                self.signed_in(self.parent).get(reverse("accounts:dashboard"))

        self.check_events(
            [
                (
                    "cross-household event 404",
                    event_404,
                    r"not found|40[34]|denied|forbidden",
                    [parent_b.username, str(parent_b.pk)],
                    [f"/events/{self.event.pk}/"],
                ),
                (
                    "cross-household members 403",
                    members_403,
                    r"forbidden|denied|permission|403",
                    [admin_b.username, str(admin_b.pk)],
                    [str(self.ws.pk)],
                ),
                (
                    "kid on parent-only route",
                    kid_route,
                    r"not found|forbidden|denied|40[34]",
                    [kid.username, str(kid.pk)],
                    [str(self.ws.pk)],
                ),
                (
                    "429 from the login limiter",
                    limiter,
                    r"too many|rate|limit|429",
                    ["x", IP],
                    ["/accounts/login/"],
                ),
                (
                    "CSRF failure",
                    csrf,
                    r"csrf",
                    [self.parent.username, str(self.parent.pk), IP],
                    ["/accounts/profile/"],
                ),
                ("DisallowedHost", host, r"host", [IP], ["evil.g4.example"]),
                (
                    "unhandled 500",
                    boom,
                    r"error|exception|500",
                    [self.parent.username, str(self.parent.pk)],
                    ["/accounts/dashboard/"],
                ),
            ]
        )

    def test_poc_GAP4_004_invite_token_is_written_to_the_log(self):
        """GAP4-004 / V14.2.1, V16.2.5 / pitfall 59: the invite link is a credential in the
        URL path. django.request logs 'Bad Request: /accounts/invite/<token>/' for a stale
        link, and axes logs path_info with the token on the request that accepts it."""
        stale = make_invite_token(self.pending)
        from accounts.services import reissue_invite

        reissue_invite(Membership.objects.get(pk=self.pending.pk))
        other = User.objects.create_user(
            username="G4Pending2", password=None, is_active=False
        )
        second = Membership.objects.create(
            workspace=self.ws,
            user=other,
            role=Membership.Role.KID,
            invited_by=self.admin,
            accepted_at=None,
        )
        live = make_invite_token(second)
        register_secret(stale)
        register_secret(live)
        with capture() as cap:
            c = Client(REMOTE_ADDR=IP)
            c.get(reverse("accounts:accept_invite", args=[stale]))
            c.post(
                reverse("accounts:accept_invite", args=[live]),
                {
                    "new_password1": "Invite-G4-Pw-7788",
                    "new_password2": "Invite-G4-Pw-7788",
                },
            )
        leaks = [
            (name, redact(line)[:150])
            for name, token in (("stale", stale), ("accepted", live))
            for line in cap.lines()
            if token in line
        ]
        self.assertFalse(leaks, f"invite token in log lines: {leaks}")


class WebSocketRefusalLogging(TransactionTestCase):
    def setUp(self):
        cache.clear()
        build_world(self)

    def test_poc_GAP4_001b_websocket_4403_leaves_no_log_record(self):
        """GAP4-001 (WebSocket part) / V16.3.2: a member of family B opening family A's chat
        socket is closed with 4403 and nothing is logged by the app; daphne's access log
        only prints 'WSREJECT <path>' with an address, never the user."""
        client = Client()
        client.force_login(self.parent_b)
        cookie = client.cookies["sessionid"].value
        register_secret(cookie)
        headers = [
            (b"host", b"localhost"),
            (b"origin", b"http://localhost:8000"),
            (b"cookie", f"sessionid={cookie}".encode()),
        ]

        async def run():
            comm = WebsocketCommunicator(
                application, f"/ws/chat/{self.ws.id}/", headers=headers
            )
            return await connect_and_check(comm)

        with capture() as cap:
            connected, code = async_to_sync(run)()
        self.assertFalse(connected)
        ok, report = judge(
            cap,
            what=r"websocket|chat|socket|4403|refus|denied|forbidden",
            who=[self.parent_b.username, str(self.parent_b.pk)],
            where=[str(self.ws.pk)],
        )
        self.assertTrue(ok, f"WS close {code}: {report}\n{describe(cap.records)}")


class LogInjectionAndPrivacy(Gap4Base):
    def test_poc_GAP4_005_mta_line_name_can_forge_a_log_line(self):
        """GAP4-005 / V16.4.1: events/trip.py:627 logs segment["line"] (upstream route data)
        with %s. CR/LF in it reaches the log as a real line break, so a poisoned upstream
        could forge a second, fully formatted log line."""
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
            trip_mod.mta, "next_departures", side_effect=ValueError("down")
        ):
            trip_mod.check_live(plan, now, trip_mod.MtaStatus())
        lines = [m for m in cap.messages() if "MTA live feed unavailable" in m]
        self.assertTrue(lines, "logger call not reached; test invalid")
        self.assertFalse(re.search(r"[\r\n\x1b]", lines[0]), repr(lines[0]))

    @override_settings(AXES_SENSITIVE_PARAMETERS=[])
    def test_poc_GAP4_006_unmasking_axes_lets_a_username_forge_log_lines(self):
        """GAP4-006 / V16.4.1 (latent): fixing GAP-002 by turning axes masking off writes the
        raw, attacker-chosen login username into axes records (5 of 6 records per attempt
        series), including CR/LF and ESC. Pair the fix with sanitising (AXES_CLIENT_STR_CALLABLE
        or a logging filter)."""
        with capture() as cap:
            for _ in range(6):
                Client().post(
                    reverse("accounts:login"),
                    {"username": "victim" + FORGED, "password": "x-g4-wrong"},
                    REMOTE_ADDR=IP,
                )
        bad = [m for m in cap.messages() if re.search(r"[\r\n\x1b]", m)]
        self.assertFalse(
            bad,
            (
                f"{len(bad)} records carry raw CR/LF/ESC, e.g. {bad[0][:120]!r}"
                if bad
                else ""
            ),
        )

    @override_settings(GOOGLE_MAPS_API_KEY="")
    def test_poc_GAP4_007_trip_failure_log_carries_home_and_event_coordinates(self):
        """GAP4-007 / V14.2.1 / pitfall 59: events/trip.py:729 logs the requests exception
        with exc_info=True; urllib3's message includes the full Transitous URL, i.e. the
        family's start point and the event location, plus a traceback."""
        from types import SimpleNamespace

        from events.models import Event

        event = Event.objects.get(pk=self.event.pk)
        event.transport_mode = Event.TransportMode.TRANSIT
        event.save(update_fields=["transport_mode"])
        origin = SimpleNamespace(
            latitude=40.689201, longitude=-73.944201, address="home"
        )
        with capture() as cap, mock.patch.object(
            trip_mod, "TRANSITOUS_URL", "http://127.0.0.1:9/plan"  # closed local port
        ):
            result = trip_mod.build_trip(event, self.admin, origin=origin)
        self.assertEqual(
            result["status"], "error", "failure path not reached; test invalid"
        )
        blob = "\n".join(cap.lines())
        self.assertNotIn("40.689201", blob)


class FailSafeAndMethodSafety(Gap4Base):
    def _connect(self, user, key):
        calendar_fields._fernet_for.cache_clear()
        with override_settings(FIELD_ENCRYPTION_KEYS=[key]):
            CalendarConnection.objects.create(
                user=user,
                workspace=self.ws if user.pk == self.admin.pk else None,
                provider=CalendarConnection.Provider.GOOGLE,
                account_email="g@gap4.example",
                access_token="ya29.gap4",
                refresh_token="1//gap4",
            )
        calendar_fields._fernet_for.cache_clear()

    def test_poc_GAP4_008_one_undecryptable_token_500s_every_page_of_that_user(self):
        """GAP4-008 / V16.5.3: calendars/fields.py:44 raises ImproperlyConfigured on a failed
        decrypt; google_calendar_context (calendars/context.py:15, run by the shell context
        processor on every page) loads the row, so every authenticated page 500s for each user
        with a Google connection after a key rotation or typo in FIELD_ENCRYPTION_KEYS.
        """
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        self._connect(self.parent, old)
        calendar_fields._fernet_for.cache_clear()
        urls = [
            reverse("accounts:dashboard"),
            reverse("events:list"),
            reverse("chat:workspace_chat", args=[self.ws.pk]),
            reverse("checklists:list", args=[self.ws.pk]),
            reverse("accounts:profile"),
        ]
        try:
            with override_settings(FIELD_ENCRYPTION_KEYS=[new]), capture() as cap:
                statuses = {
                    u: self.signed_in(self.parent).get(u).status_code for u in urls
                }
                kid_status = (
                    self.signed_in(self.kid)
                    .get(reverse("accounts:dashboard"))
                    .status_code
                )
        finally:
            calendar_fields._fernet_for.cache_clear()
        self.assertEqual(kid_status, 200)
        self.assertTrue(
            any("can't be decrypted" in r.gap4_text for r in cap.records), "wrong cause"
        )
        self.assertFalse(
            [u for u, s in statuses.items() if s >= 500],
            f"pages answering 5xx after a decrypt failure: {statuses}",
        )

    def test_poc_GAP4_009_user_cannot_disconnect_an_undecryptable_connection(self):
        """GAP4-009 / V16.5.3: with an undecryptable token the Disconnect action also 500s
        (the view loads the row), so the user cannot repair it from the UI."""
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        self._connect(self.parent, old)
        calendar_fields._fernet_for.cache_clear()
        try:
            with override_settings(FIELD_ENCRYPTION_KEYS=[new]), mock.patch(
                "calendars.google.revoke"
            ):
                r = self.signed_in(self.parent).post(
                    reverse("calendars:google_disconnect")
                )
                gone = not CalendarConnection.objects.filter(user=self.parent).exists()
        finally:
            calendar_fields._fernet_for.cache_clear()
        self.assertLess(r.status_code, 500)
        self.assertTrue(gone)

    # Open: make two-step setup POST-only together with the tests that read its device. Audit REPORT.md (h).
    @unittest.expectedFailure
    def test_poc_GAP4_010_get_two_step_setup_writes_a_row(self):
        """GAP4-010 / V3.5.3, V15.3.7: GET /accounts/two-step/setup/ creates an unconfirmed
        TOTPDevice (accounts/two_step.py:129-133). A cross-site link can trigger the write.
        """
        client = self.signed_in(self.parent)
        before = snapshot()
        client.get(reverse("accounts:two_step_setup"))
        self.assertEqual(before, snapshot())

    def test_poc_GAP4_011_json_route_500_is_an_html_page(self):
        """GAP4-011 / V16.5.1 (info): a 500 on a JSON route (chat message_page) returns the
        HTML error page; the chat script's response.json() then throws instead of showing a
        message. No internals leak (that part is a control)."""
        with mock.patch(
            "chat.views.get_message_page", side_effect=RuntimeError("g4-internal")
        ):
            r = self.signed_in(self.parent).get(
                reverse("chat:messages", args=[self.ws.pk]),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("g4-internal", r.content.decode())
        self.assertIn("json", r.headers.get("Content-Type", ""))

    def test_control_GAP4_tokenless_posts_are_refused_and_get_head_are_safe(self):
        """Control (V3.5.1, V3.5.3, pitfall 77): a tokenless POST to a state-changing route is
        a 403 with no DB change, and GET/HEAD with the POST parameters in the query string
        changes nothing, on a sample covering every app. The full 64-route sweep runs in
        test_logging_csrf.CsrfAndMethodSweepTests."""
        client = Client(
            enforce_csrf_checks=True, raise_request_exception=False, REMOTE_ADDR=IP
        )
        client.force_login(self.admin)
        samples = [
            (reverse("events:delete", args=[self.event.pk]), {"action": "delete"}),
            (reverse("accounts:remove_member", args=[self.ws.pk, self.pending.pk]), {}),
            (
                reverse("checklists:item_toggle", args=[self.item.pk]),
                {"completed": "1"},
            ),
            (reverse("calendars:google_sync"), {}),
            (reverse("accounts:workspace_detail", args=[self.ws.pk]), {"name": "x"}),
            (reverse("chat:workspace_chat", args=[self.ws.pk]), {"content": "x"}),
        ]
        for url, data in samples:
            before = snapshot()
            self.assertEqual(client.post(url, data).status_code, 403, url)
            for verb in ("get", "head"):
                getattr(client, verb)(f"{url}?{urlencode(data)}")
            self.assertEqual(before, snapshot(), url)
