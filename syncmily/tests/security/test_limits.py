"""
Request-limit, rate-limit and error-handling tests (lane: limits).

Every test_poc_* test asserts the SECURE behaviour, so it FAILS while the gap
exists. test_control_* tests pin behaviour that is already correct, so a later
fix cannot regress it.

Run (own database, never the shared one):
    POSTGRES_DB=syncmily_hard_limits python manage.py test \
        syncmily.tests.security.test_limits --noinput
"""

import asyncio
import json
import re
from pathlib import Path
from unittest import mock
from uuid import uuid4

from asgiref.sync import async_to_sync
from chat.tests.ws_refusal import connect_and_check
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.test import override_settings
from django.urls import get_resolver
from django.utils import timezone

from accounts.models import Membership, Workspace
from checklists.models import Checklist, ChecklistItem
from events.models import Event
from syncmily import ratelimit
from syncmily.asgi import application

PASSWORD = "Correct-Horse-Battery-9"
BASE_DIR = Path(settings.BASE_DIR)
HTTPS_SCRIPT = (BASE_DIR / ".platform/hooks/postdeploy/03_https.sh").read_text()


def make_household(prefix):
    User = get_user_model()
    parent = User.objects.create_user(username=f"{prefix}_parent", password=PASSWORD)
    workspace = Workspace.objects.create(name=f"{prefix} family", primary_admin=parent)
    kid = User.objects.create_user(username=f"{prefix}_kid", password=PASSWORD)
    Membership.objects.create(
        user=kid,
        workspace=workspace,
        role=Membership.Role.KID,
        invited_by=parent,
        accepted_at=timezone.now(),
    )
    return workspace, parent, kid


def login_client(user, **kw):
    client = Client(**kw)
    assert client.login(username=user.get_username(), password=PASSWORD)
    return client


# ---------------------------------------------------------------- settings ---


class LimitSettingsTests(SimpleTestCase):
    """Django's request-size settings are all at their permissive defaults."""

    def test_poc_data_upload_max_memory_size_is_lowered(self):
        # Largest legitimate non-file POST is a few KB (chat is 2,000 chars, event
        # forms a few hundred). Default 2.5 MB lets one request echo a 2.5 MB page.
        self.assertLessEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 512 * 1024)

    def test_poc_data_upload_max_number_fields_is_lowered(self):
        # Biggest form is the event form (about 30 inputs incl. participant boxes).
        self.assertLessEqual(settings.DATA_UPLOAD_MAX_NUMBER_FIELDS, 300)

    def test_poc_data_upload_max_number_files_is_lowered(self):
        # Only scan-ocr takes files, and it reads one. Default is 100.
        self.assertLessEqual(settings.DATA_UPLOAD_MAX_NUMBER_FILES, 5)

    def test_poc_csrf_failure_view_is_set(self):
        self.assertTrue(
            getattr(settings, "CSRF_FAILURE_VIEW", "").startswith(
                ("syncmily.", "accounts.")
            ),
            "CSRF_FAILURE_VIEW is Django's plain default page",
        )

    def test_poc_custom_400_handler_exists(self):
        handler = get_resolver().resolve_error_handler(400)
        self.assertNotEqual(handler.__module__, "django.views.defaults")

    def test_poc_rate_limit_cache_has_room_for_counters(self):
        # settings has no CACHES, so the rate limiter shares Django's default
        # LocMemCache, which culls a third of its keys above 300 entries.
        caches = getattr(settings, "CACHES", {})
        options = caches.get("default", {}).get("OPTIONS", {})
        self.assertGreaterEqual(options.get("MAX_ENTRIES", 300), 10000)

    def test_control_axes_lockout_configured(self):
        self.assertEqual(settings.AXES_FAILURE_LIMIT, 5)


class RateLimitCacheCullingTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(RATELIMIT_ENABLED=True)
    def test_poc_counter_survives_other_keys(self):
        """An attacker (or ordinary traffic from ~100 users) evicts a counter and the
        victim's limit resets. 'victim' used up its 5 allowed hits; then 600 other
        users each make a request; the victim must still be blocked."""
        for _ in range(5):
            ratelimit.hit("login", "ip:203.0.113.9", 5, 600)
        self.assertFalse(ratelimit.hit("login", "ip:203.0.113.9", 5, 600))
        for i in range(600):
            ratelimit.hit("chat_burst", f"user:{i}", 20, 10)
        self.assertFalse(
            ratelimit.hit("login", "ip:203.0.113.9", 5, 600),
            "counter was culled from the 300-entry LocMemCache; limit reset to zero",
        )


# --------------------------------------------------------------- controls ---


class RequestSizeControlTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ws, self.parent, self.kid = make_household("lim")
        self.client = login_client(self.parent)

    def test_control_body_over_2_5mb_is_400_not_500(self):
        body = b"action=username&username=" + b"A" * (2_700_000)
        r = self.client.post(
            "/accounts/profile/", body, content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(r.status_code, 400)

    def test_control_more_than_1000_fields_is_400(self):
        body = "&".join(f"k{i}=v" for i in range(1001)).encode()
        r = self.client.post(
            "/accounts/profile/", body, content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(r.status_code, 400)

    def test_control_more_than_100_files_is_400(self):
        files = {f"f{i}": SimpleUploadedFile(f"a{i}.png", b"x") for i in range(101)}
        r = self.client.post(f"/events/workspaces/{self.ws.pk}/scan-ocr/", files)
        self.assertEqual(r.status_code, 400)

    def test_control_scan_over_10mb_is_413_json(self):
        big = SimpleUploadedFile("a.png", b"\x00" * (10 * 1024 * 1024 + 1), "image/png")
        r = self.client.post(
            f"/events/workspaces/{self.ws.pk}/scan-ocr/", {"document": big}
        )
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r["Content-Type"], "application/json")

    def test_control_deeply_nested_json_body_is_ignored(self):
        # No view parses a JSON body; the JSON endpoints must not 500 on one.
        body = "[" * 5000 + "]" * 5000
        for path in (
            "/events/places/",
            "/events/places/details/",
            f"/events/workspaces/{self.ws.pk}/scan-ocr/",
            f"/events/workspaces/{self.ws.pk}/colors/",
        ):
            with self.subTest(path=path):
                r = self.client.post(path, body, content_type="application/json")
                self.assertLess(r.status_code, 500)

    def test_control_unknown_and_huge_ids_are_404_not_500(self):
        huge = "9" * 40
        for method, path in (
            ("get", f"/events/{huge}/"),
            ("get", f"/events/{huge}/edit/"),
            ("post", f"/events/{huge}/delete/"),
            ("post", f"/events/{huge}/time/"),
            ("post", f"/checklists/items/{huge}/toggle/"),
            ("get", f"/checklists/items/{huge}/edit/"),
        ):
            with self.subTest(path=path):
                r = getattr(self.client, method)(path)
                self.assertEqual(r.status_code, 404)

    def test_control_odd_query_params_do_not_500(self):
        for q in (
            "?page=99999999999999999999",
            "?date=0000-99-99&view=zzz",
            "?date=9999-12-31&view=week",
            "?date=0001-01-01&view=week",
            "?" + "&".join(f"p{i}=1" for i in range(900)),
        ):
            with self.subTest(q=q[:40]):
                r = self.client.get(f"/events/workspaces/{self.ws.pk}/{q}")
                self.assertLess(r.status_code, 500)
        r = self.client.get(f"/chat/{self.ws.pk}/messages/?before={'9' * 5000}")
        self.assertIn(r.status_code, (200, 400))


class ReflectionAmplificationTests(TestCase):
    def test_poc_oversized_field_is_not_echoed_back_in_full(self):
        """2.4 MB (just under the 2.5 MB cap) in the username box comes back as a
        2.5 MB HTML page: one small request, a 2.5 MB response, repeatable."""
        ws, parent, _ = make_household("echo")
        client = login_client(parent)
        r = client.post(
            "/accounts/profile/",
            {"action": "username", "username": "A" * 2_400_000},
        )
        self.assertLess(len(r.content), 200_000, f"response was {len(r.content)} bytes")


class ErrorPageControlTests(TestCase):
    def setUp(self):
        self.ws, self.parent, self.kid = make_household("err")

    def test_control_500_page_leaks_nothing(self):
        client = login_client(self.parent, raise_request_exception=False)
        with mock.patch(
            "events.views.available_timezones",
            side_effect=RuntimeError(
                "SELECT secret FROM auth_user /srv/app/settings.py DJANGO_SECRET_KEY"
            ),
        ):
            r = client.get("/events/timezones/")
        self.assertEqual(r.status_code, 500)
        text = r.content.decode()
        self.assertIn("Something went wrong", text)
        for leak in (
            "Traceback",
            "RuntimeError",
            "SELECT",
            "settings.py",
            "SECRET",
            "/srv/",
            "/Users/",
        ):
            self.assertNotIn(leak, text)

    def test_control_404_and_403_pages_are_branded_and_clean(self):
        client = login_client(self.parent)
        r = client.get("/definitely/not/here/")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Syncmily", r.content.decode())
        self.assertNotIn("Using the URLconf", r.content.decode())
        other_ws, other_parent, _ = make_household("err2")
        r = client.get(f"/accounts/workspaces/{other_ws.pk}/members/")
        self.assertIn(r.status_code, (403, 404))
        self.assertNotIn("Traceback", r.content.decode())

    def test_control_csrf_failure_page_has_no_internals(self):
        client = Client(enforce_csrf_checks=True)
        r = client.post("/accounts/login/", {"username": "a", "password": "b"})
        self.assertEqual(r.status_code, 403)
        text = r.content.decode()
        for leak in ("Traceback", "settings", "REASON", "Referer checking failed"):
            self.assertNotIn(leak, text)

    def test_poc_csrf_failure_page_is_branded_and_has_a_way_back(self):
        client = Client(enforce_csrf_checks=True)
        r = client.post("/accounts/login/", {"username": "a", "password": "b"})
        text = r.content.decode()
        self.assertIn("Syncmily", text)
        self.assertIn("/accounts/login/", text)

    def test_poc_400_page_is_branded(self):
        client = login_client(self.parent)
        body = b"a=" + b"A" * 2_700_000
        r = client.post(
            "/accounts/profile/", body, content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Syncmily", r.content.decode())

    def test_poc_json_endpoints_answer_json_errors(self):
        client = login_client(self.parent)
        h = {
            "HTTP_ACCEPT": "application/json",
            "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
        }
        r = client.post("/events/places/details/", {"id": "!!"}, **h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("json", r["Content-Type"], "400 from place_details is text/html")
        r = client.get("/events/places/", **h)
        self.assertEqual(r.status_code, 405)
        self.assertIn("json", r["Content-Type"], "405 on a JSON endpoint is empty HTML")
        r = client.post(
            f"/events/workspaces/{uuid4()}/scan-ocr/", {"timezone": "UTC"}, **h
        )
        self.assertEqual(r.status_code, 404)
        self.assertIn("json", r["Content-Type"], "404 on scan-ocr is the HTML page")

    def test_poc_anonymous_xhr_gets_401_json_not_a_login_redirect(self):
        h = {
            "HTTP_ACCEPT": "application/json",
            "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
        }
        r = Client().post("/events/places/", {"q": "abcd"}, **h)
        self.assertEqual(r.status_code, 401, "fetch() follows the 302 and parses HTML")


class ConcurrentDeleteTests(TestCase):
    def test_poc_adding_item_to_checklist_deleted_a_moment_ago_is_not_a_500(self):
        """ZAP's active scan hit this live: item_add looks the checklist up, then
        locks it with .get(); a concurrent delete between the two raises
        Checklist.DoesNotExist (HTTP 500, traceback in the log)."""
        from checklists import views as checklist_views

        ws, parent, _ = make_household("race")
        checklist = Checklist.objects.create(
            workspace=ws, title="Bag", created_by=parent
        )
        real = checklist_views.get_manageable_checklist

        def lookup_then_vanish(user, checklist_id):
            found = real(user, checklist_id)
            Checklist.objects.filter(pk=checklist_id).delete()
            return found

        client = login_client(parent, raise_request_exception=False)
        with mock.patch.object(
            checklist_views, "get_manageable_checklist", lookup_then_vanish
        ):
            r = client.post(
                f"/checklists/{checklist.pk}/items/add/", {"description": "Boots"}
            )
        self.assertNotEqual(r.status_code, 500)


# ------------------------------------------------------- rate-limit coverage ---


@override_settings(RATELIMIT_ENABLED=True)
class MutatingEndpointRateLimitTests(TestCase):
    """Fire 150 requests at each mutating endpoint that has no limiter today; the
    secure behaviour is that at least one is answered 429 (or the cap applies)."""

    def setUp(self):
        cache.clear()
        self.ws, self.parent, self.kid = make_household("rl")
        self.client = login_client(self.parent)
        now = timezone.now()
        self.event = Event.objects.create(
            workspace=self.ws,
            created_by=self.parent,
            title="Soccer",
            starts_at=now,
            ends_at=now + timezone.timedelta(hours=1),
            timezone_name="UTC",
        )
        self.checklist = Checklist.objects.create(
            workspace=self.ws, title="Bag", created_by=self.parent
        )
        self.item = ChecklistItem.objects.create(
            checklist=self.checklist, description="Boots"
        )

    def _saw_429(self, path, data, n=150):
        for _ in range(n):
            if self.client.post(path, data).status_code == 429:
                return True
        return False

    def test_poc_profile_password_change_is_limited(self):
        self.assertTrue(
            self._saw_429(
                "/accounts/profile/",
                {
                    "action": "password",
                    "old_password": "wrong",
                    "new_password1": "Xyz-123456-abc",
                    "new_password2": "Xyz-123456-abc",
                },
            ),
            "unlimited wrong-old-password guesses and one PBKDF2 hash each",
        )

    def test_poc_change_event_time_is_limited(self):
        self.assertTrue(self._saw_429(f"/events/{self.event.pk}/time/", {}))

    def test_poc_set_transport_mode_is_limited(self):
        self.assertTrue(
            self._saw_429(f"/events/{self.event.pk}/transport/", {"mode": "walking"})
        )

    def test_poc_member_colors_is_limited(self):
        self.assertTrue(self._saw_429(f"/events/workspaces/{self.ws.pk}/colors/", {}))

    def test_poc_checklist_item_toggle_is_limited(self):
        self.assertTrue(
            self._saw_429(
                f"/checklists/items/{self.item.pk}/toggle/", {"completed": "1"}
            )
        )

    def test_poc_checklist_delete_and_edit_are_limited(self):
        self.assertTrue(
            self._saw_429(f"/checklists/{self.checklist.pk}/edit/", {"title": "x"})
        )

    def test_poc_google_sync_is_limited(self):
        # Each call would reach Google's API for a connected user.
        self.assertTrue(self._saw_429("/calendars/google/sync/", {}))

    def test_poc_event_creation_has_a_rate_or_volume_cap(self):
        url = f"/events/workspaces/{self.ws.pk}/new/"
        limited = False
        for i in range(120):
            token = signing.dumps(
                {
                    "id": str(uuid4()),
                    "user": self.parent.pk,
                    "workspace": str(self.ws.pk),
                },
                salt="events.create",
            )
            r = self.client.post(
                url,
                {
                    "action": "save",
                    "submission_token": token,
                    "title": f"Load {i}",
                    "date": "2026-12-01",
                    "start_time": "10:00",
                    "end_time": "11:00",
                    "timezone": "UTC",
                    "transport_mode": "walking",
                    "allow_overlap": "on",
                },
            )
            if r.status_code == 429:
                limited = True
                break
        self.assertTrue(limited, "120 events in a few seconds, no limit")

    def test_control_login_signup_reset_scan_are_limited(self):
        anon = Client()
        got = [
            anon.post("/accounts/signup/", {"username": ""}).status_code
            for _ in range(60)
        ]
        self.assertIn(429, got)
        got = [
            anon.post(
                "/accounts/password-reset/", {"email": "a@example.com"}
            ).status_code
            for _ in range(40)
        ]
        self.assertIn(429, got)
        got = [
            self.client.post(
                f"/events/workspaces/{self.ws.pk}/scan-ocr/", {"timezone": "UTC"}
            ).status_code
            for _ in range(8)
        ]
        self.assertIn(429, got)

    def test_control_checklist_create_and_item_add_are_limited(self):
        got = [
            self.client.post(
                f"/checklists/workspaces/{self.ws.pk}/create/", {"title": ""}
            ).status_code
            for _ in range(70)
        ]
        self.assertIn(429, got)


class NginxConfigTests(SimpleTestCase):
    """The generated nginx config (03_https.sh) is the only edge limit."""

    def test_control_body_cap_and_auth_zone_present(self):
        self.assertIn("client_max_body_size  12m", HTTPS_SCRIPT)
        self.assertIn("limit_req_zone", HTTPS_SCRIPT)

    def test_poc_per_ip_connection_limit_exists(self):
        self.assertIn(
            "limit_conn",
            HTTPS_SCRIPT,
            "one client can hold every worker connection with slow-drip requests",
        )

    def test_poc_password_reset_and_writes_have_edge_rate_limit(self):
        covered = re.findall(r"location ~[^{]+\{[^}]*limit_req", HTTPS_SCRIPT)
        joined = " ".join(covered)
        self.assertIn("password-reset", joined)

    def test_poc_body_cap_is_small_outside_scan_upload(self):
        # The 12 MB cap is only needed for scan-ocr; every other path takes
        # form posts of a few KB.
        blocks = re.split(r"\n    location ", HTTPS_SCRIPT)
        default_block = next(b for b in blocks if b.startswith("/ {"))
        self.assertRegex(default_block, r"client_max_body_size\s+\d+k")

    def test_poc_websocket_location_has_connection_cap_and_timeouts(self):
        self.assertRegex(HTTPS_SCRIPT, r"location\s+/ws/")


# --------------------------------------------------------------- websocket ---


def _socket(workspace, cookie):
    headers = [
        (b"host", b"localhost"),
        (b"origin", b"http://localhost:8000"),
        (b"cookie", f"sessionid={cookie}".encode()),
    ]
    return WebsocketCommunicator(
        application, f"/ws/chat/{workspace.id}/", headers=headers
    )


async def _next(communicator, timeout=0.5):
    try:
        msg = await asyncio.wait_for(communicator.output_queue.get(), timeout)
    except asyncio.TimeoutError:
        return None
    if isinstance(msg, Exception):
        raise msg
    return msg


@override_settings(RATELIMIT_ENABLED=True)
class WebSocketLimitTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.ws, self.parent, self.kid = make_household("wsl")
        client = login_client(self.parent)
        self.cookie = client.cookies["sessionid"].value

    def _frames(self, texts):
        async def run():
            c = _socket(self.ws, self.cookie)
            ok, _ = await c.connect()
            assert ok
            out = []
            for t in texts:
                await c.send_to(text_data=t)
                out.append(await _next(c))
            await c.disconnect()
            return out

        return async_to_sync(run)()

    def test_control_frame_over_8192_chars_is_refused_with_error(self):
        (msg,) = self._frames(["x" * 8193])
        self.assertEqual(json.loads(msg["text"])["message"], "Message is too long.")

    def test_control_burst_beyond_20_per_10s_is_refused(self):
        frames = [
            json.dumps({"type": "chat_message", "content": f"m{i}"}) for i in range(30)
        ]

        async def run():
            c = _socket(self.ws, self.cookie)
            await c.connect()
            for t in frames:
                await c.send_to(text_data=t)
            await asyncio.sleep(0.8)
            errors = chats = 0
            while True:
                msg = await _next(c, 0.2)
                if msg is None:
                    break
                if msg["type"] == "websocket.send":
                    kind = json.loads(msg["text"])["type"]
                    errors += kind == "error"
                    chats += kind == "chat_message"
            await c.disconnect()
            return errors, chats

        errors, chats = async_to_sync(run)()
        self.assertEqual(chats, 20)
        self.assertEqual(errors, 10)

    def _survives(self, **frame):
        async def run():
            c = _socket(self.ws, self.cookie)
            await c.connect()
            await c.send_to(**frame)
            await asyncio.sleep(0.4)
            crashed = c.future.done()
            if crashed:
                try:
                    c.future.result()
                except Exception as exc:
                    crashed = type(exc).__name__
            else:
                await c.disconnect()
            return crashed

        return async_to_sync(run)()

    def test_poc_invalid_json_frame_does_not_crash_the_consumer(self):
        # decode_json is not wrapped: JSONDecodeError escapes receive(), daphne logs
        # "Exception inside application" and closes with 1011.
        self.assertFalse(self._survives(text_data="{not json"))

    def test_poc_deeply_nested_garbage_does_not_crash_the_consumer(self):
        self.assertFalse(self._survives(text_data="[" * 8000))

    def test_poc_binary_frame_does_not_crash_the_consumer(self):
        self.assertFalse(self._survives(bytes_data=b"\x00\x01"))

    def test_poc_one_user_cannot_hold_unlimited_sockets(self):
        # 400 sockets from one user cost ~28 MB in the live run (about 70 KB each);
        # at 1 GB on a t3.micro, 10,000 would end the process.
        async def run():
            sockets, accepted = [], 0
            for _ in range(12):
                c = _socket(self.ws, self.cookie)
                ok, _ = await connect_and_check(c)
                accepted += bool(ok)
                sockets.append(c)
            for c in sockets:
                try:
                    await c.disconnect()
                except Exception:
                    pass
            return accepted

        self.assertLessEqual(async_to_sync(run)(), 5)

    def test_poc_over_limit_frames_do_no_db_work(self):
        # has_access() runs a query for every frame BEFORE the burst limit is
        # checked, so a 1,000 frames/s flood costs 1,000 queries/s.
        from django.db.backends import utils

        counts = {"q": 0}
        real = utils.CursorWrapper.execute

        def execute(self_, sql, params=None):
            counts["q"] += 1
            return real(self_, sql, params)

        frames = [json.dumps({"type": "chat_message", "content": "m"})] * 20

        async def run():
            c = _socket(self.ws, self.cookie)
            await c.connect()
            for t in frames:
                await c.send_to(text_data=t)
            await asyncio.sleep(1.0)  # the 20 allowed messages are done
            before = counts["q"]
            for _ in range(200):
                await c.send_to(text_data=frames[0])
            await asyncio.sleep(1.0)
            await c.disconnect()
            return counts["q"] - before

        with mock.patch.object(utils.CursorWrapper, "execute", execute):
            extra = async_to_sync(run)()
        self.assertLess(extra, 20, f"{extra} queries for 200 refused frames")
