"""
Security PoC tests for the household chat WebSocket (lane ws).

Every test_poc_* test asserts the SECURE behaviour, so it FAILS while the bug
exists. test_control_* tests pin behaviour that is already correct, so a later
fix cannot regress it. All sockets use REAL sessions (session cookie in the
handshake), not a pre-set scope["user"], so AuthMiddlewareStack really runs.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest import mock

from asgiref.sync import async_to_sync
from chat.tests.ws_refusal import connect_and_check
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.backends.postgresql.base import DatabaseWrapper
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import Membership, Workspace
from chat.models import ChatMessage
from syncmily.asgi import application

PASSWORD = "Correct-Horse-Battery-9"
GOOD_ORIGIN = b"http://localhost:8000"


def make_household(prefix, with_kid=True):
    User = get_user_model()
    parent = User.objects.create_user(username=f"{prefix}_parent", password=PASSWORD)
    workspace = Workspace.objects.create(name=f"{prefix} family", primary_admin=parent)
    kid = None
    if with_kid:
        kid = User.objects.create_user(username=f"{prefix}_kid", password=PASSWORD)
        Membership.objects.create(
            user=kid,
            workspace=workspace,
            role=Membership.Role.KID,
            invited_by=parent,
            accepted_at=timezone.now(),
        )
    return workspace, parent, kid


def login_client(user):
    """A Django test client with a real session; returns (client, session cookie)."""
    client = Client()
    assert client.login(username=user.get_username(), password=PASSWORD)
    return client, client.cookies["sessionid"].value


def socket_for(workspace, cookie=None, origin=GOOD_ORIGIN, host=b"localhost"):
    headers = [(b"host", host)]
    if origin is not None:
        headers.append((b"origin", origin))
    if cookie is not None:
        headers.append((b"cookie", f"sessionid={cookie}".encode()))
    return WebsocketCommunicator(
        application, f"/ws/chat/{workspace.id}/", headers=headers
    )


async def _next_output(communicator, timeout):
    """Next ASGI output event, or None on timeout. Unlike receive_output, a timeout
    does not cancel the application, so the socket stays usable afterwards."""
    try:
        msg = await asyncio.wait_for(communicator.output_queue.get(), timeout)
    except asyncio.TimeoutError:
        return None
    if isinstance(msg, Exception):
        raise msg
    return msg


async def drain(communicator, timeout=0.3):
    """Every JSON text frame the socket has queued."""
    out = []
    while True:
        msg = await _next_output(communicator, timeout)
        if msg is None or msg["type"] == "websocket.close":
            return out
        if msg["type"] == "websocket.send" and msg.get("text"):
            out.append(json.loads(msg["text"]))


async def is_closed(communicator, timeout=1.0):
    """True when the server closes the socket (a websocket.close event arrives)."""
    while True:
        msg = await _next_output(communicator, timeout)
        if msg is None:
            return False
        if msg["type"] == "websocket.close":
            return True


@contextmanager
def count_db_work():
    """Count SQL statements and new DB connections across ALL threads."""
    counts = {"queries": 0, "connections": 0}
    from django.db.backends import utils

    real_exec = utils.CursorWrapper.execute
    real_conn = DatabaseWrapper.get_new_connection

    def execute(self, sql, params=None):
        counts["queries"] += 1
        return real_exec(self, sql, params)

    def new_conn(self, params):
        counts["connections"] += 1
        return real_conn(self, params)

    with mock.patch.object(utils.CursorWrapper, "execute", execute), mock.patch.object(
        DatabaseWrapper, "get_new_connection", new_conn
    ):
        yield counts


async def in_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


class WsBase(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.ws_a, self.parent_a, self.kid_a = make_household("wsa")
        self.ws_b, self.parent_b, self.kid_b = make_household("wsb")


class HandshakeAccessTests(WsBase):
    def _connects(self, workspace, cookie, **kw):
        async def run():
            c = socket_for(workspace, cookie, **kw)
            ok, _ = await connect_and_check(c)
            if ok:
                await c.disconnect()
            return ok

        return async_to_sync(run)()

    def test_control_member_connects_with_real_session(self):
        _, cookie = login_client(self.kid_a)
        self.assertTrue(self._connects(self.ws_a, cookie))

    def test_control_other_household_join_refused(self):
        _, cookie = login_client(self.kid_b)
        self.assertFalse(self._connects(self.ws_a, cookie))

    def test_control_anonymous_refused(self):
        self.assertFalse(self._connects(self.ws_a, None))

    def test_control_missing_origin_refused(self):
        _, cookie = login_client(self.kid_a)
        self.assertFalse(self._connects(self.ws_a, cookie, origin=None))

    def test_control_foreign_origin_refused(self):
        _, cookie = login_client(self.kid_a)
        for origin in (
            b"https://evil.example",
            b"https://localhost.evil.example",
            b"null",
            b"https://syncmily.app.evil.example",
        ):
            with self.subTest(origin=origin):
                self.assertFalse(self._connects(self.ws_a, cookie, origin=origin))

    def test_control_pending_invitee_refused(self):
        invitee = get_user_model().objects.create_user(
            username="pending_x", password=PASSWORD
        )
        Membership.objects.create(
            user=invitee,
            workspace=self.ws_a,
            role=Membership.Role.KID,
            invited_by=self.parent_a,
            accepted_at=None,
        )
        _, cookie = login_client(invitee)
        self.assertFalse(self._connects(self.ws_a, cookie))

    def test_control_stale_session_after_password_change_refused_at_handshake(self):
        _, cookie = login_client(self.kid_a)
        self.kid_a.set_password("A-Brand-New-Passw0rd!")
        self.kid_a.save()
        self.assertFalse(self._connects(self.ws_a, cookie))

    def test_control_group_isolated_between_households(self):
        _, cookie_a = login_client(self.kid_a)
        _, cookie_b = login_client(self.kid_b)

        async def run():
            ca = socket_for(self.ws_a, cookie_a)
            cb = socket_for(self.ws_b, cookie_b)
            await ca.connect()
            await cb.connect()
            await ca.send_json_to({"type": "chat_message", "content": "secret-A"})
            got_a = await drain(ca)
            got_b = await drain(cb)
            await ca.disconnect()
            await cb.disconnect()
            return got_a, got_b

        got_a, got_b = async_to_sync(run)()
        self.assertEqual([m["content"] for m in got_a], ["secret-A"])
        self.assertEqual(got_b, [])

    def test_control_removed_member_loses_open_socket(self):
        _, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            await c.connect()
            await in_thread(lambda: Membership.objects.filter(user=self.kid_a).delete())
            await c.send_json_to({"type": "chat_message", "content": "after removal"})
            return await is_closed(c)

        self.assertTrue(async_to_sync(run)())
        self.assertFalse(ChatMessage.objects.filter(content="after removal").exists())

    def test_control_removed_member_stops_receiving(self):
        _, ck = login_client(self.kid_a)
        _, cp = login_client(self.parent_a)

        async def run():
            kid = socket_for(self.ws_a, ck)
            parent = socket_for(self.ws_a, cp)
            await kid.connect()
            await parent.connect()
            await in_thread(lambda: Membership.objects.filter(user=self.kid_a).delete())
            await parent.send_json_to(
                {"type": "chat_message", "content": "private-after"}
            )
            await drain(parent)
            return await drain(kid)

        frames = async_to_sync(run)()
        self.assertFalse(any(f.get("content") == "private-after" for f in frames))


class SessionLifetimeTests(WsBase):
    """An open socket must not outlive the session that opened it (ASVS 7.4, 7.2)."""

    def _send_after(self, mutate, content):
        client, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            self.assertTrue((await c.connect())[0])
            await in_thread(mutate, client)
            await c.send_json_to({"type": "chat_message", "content": content})
            return await is_closed(c, timeout=1.0)

        return async_to_sync(run)()

    def test_poc_ws_01_logout_must_close_open_socket(self):
        """WS-01: logout deletes the session but the open socket keeps posting."""
        closed = self._send_after(lambda cl: cl.logout(), "posted after logout")
        self.assertTrue(closed, "socket stayed open after logout")
        self.assertFalse(
            ChatMessage.objects.filter(content="posted after logout").exists(),
            "message from a logged-out session was stored",
        )

    def test_poc_ws_01_logged_out_socket_must_not_receive(self):
        """WS-01: after logout the old socket still receives household chat live."""
        client, cookie = login_client(self.kid_a)
        _, pcookie = login_client(self.parent_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            p = socket_for(self.ws_a, pcookie)
            await c.connect()
            await p.connect()
            await in_thread(client.logout)
            await p.send_json_to(
                {"type": "chat_message", "content": "for members only"}
            )
            await drain(p)
            return await drain(c)

        frames = async_to_sync(run)()
        self.assertFalse(
            any(f.get("content") == "for members only" for f in frames),
            "logged-out socket still receives live household messages",
        )

    def test_poc_ws_02_password_change_must_close_open_socket(self):
        """WS-02: a password change does not end already-open sockets of that user."""

        def change_password(_client):
            u = get_user_model().objects.get(pk=self.kid_a.pk)
            u.set_password("Totally-New-Passw0rd!")
            u.save()

        closed = self._send_after(change_password, "posted after pw change")
        self.assertTrue(closed, "socket stayed open after password change")
        self.assertFalse(
            ChatMessage.objects.filter(content="posted after pw change").exists()
        )

    def test_poc_ws_03_deactivated_user_must_lose_open_socket(self):
        """WS-03: is_active is read from the connect-time snapshot, never re-read."""

        def deactivate(_client):
            get_user_model().objects.filter(pk=self.kid_a.pk).update(is_active=False)

        closed = self._send_after(deactivate, "posted while deactivated")
        self.assertTrue(closed, "deactivated account kept an authorised socket")
        self.assertFalse(
            ChatMessage.objects.filter(content="posted while deactivated").exists()
        )

    def test_poc_ws_03_deactivated_user_must_not_receive(self):
        """WS-03: a deactivated account still receives live household chat."""
        _, ck = login_client(self.kid_a)
        _, cp = login_client(self.parent_a)

        async def run():
            k = socket_for(self.ws_a, ck)
            p = socket_for(self.ws_a, cp)
            await k.connect()
            await p.connect()
            await in_thread(
                lambda: get_user_model()
                .objects.filter(pk=self.kid_a.pk)
                .update(is_active=False)
            )
            await p.send_json_to(
                {"type": "chat_message", "content": "deactivated reads this"}
            )
            await drain(p)
            return await drain(k)

        frames = async_to_sync(run)()
        self.assertFalse(
            any(f.get("content") == "deactivated reads this" for f in frames)
        )

    def test_poc_ws_04_socket_lifetime_must_be_bounded(self):
        """
        WS-04: the Procfile runs daphne with the default websocket_timeout (86400 s)
        and sockets are never re-validated, so a revoked session's socket can live
        up to 24 h. Assert that a cap of at most one hour is configured.
        """
        import re
        from pathlib import Path

        procfile = Path(__file__).resolve().parents[3] / "Procfile"
        m = re.search(r"--websocket[_-]timeout[ =](\d+)", procfile.read_text())
        self.assertIsNotNone(
            m, "Procfile sets no --websocket_timeout (daphne default 86400 s)"
        )
        self.assertLessEqual(int(m.group(1)), 7200)


class MalformedFrameTests(WsBase):
    def _probe(self, **send_kwargs):
        _, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            await c.connect()
            if send_kwargs.get("text_data") == "":
                # send_to() refuses an empty string; send the raw ASGI event instead
                await c.send_input({"type": "websocket.receive", "text": ""})
            else:
                await c.send_to(**send_kwargs)
            try:
                out = await c.receive_output(timeout=1.0)
                return ("output", out)
            except asyncio.TimeoutError:
                return ("silent", None)
            except BaseException as exc:  # app exception surfaced by the communicator
                return ("exception", repr(exc))

        return async_to_sync(run)()

    def test_poc_ws_05_binary_frame_must_not_raise(self):
        """WS-05: a binary frame raises ValueError inside the consumer (unhandled)."""
        kind, detail = self._probe(bytes_data=b"\x00\x01\x02")
        self.assertNotEqual(kind, "exception", detail)

    def test_poc_ws_05_invalid_json_must_not_raise(self):
        """WS-05: text that is not JSON raises JSONDecodeError inside the consumer."""
        kind, detail = self._probe(text_data="{not json")
        self.assertNotEqual(kind, "exception", detail)

    def test_poc_ws_05_empty_text_frame_must_not_raise(self):
        """WS-05: an empty text frame hits Channels' 'No text section' ValueError."""
        kind, detail = self._probe(text_data="")
        self.assertNotEqual(kind, "exception", detail)

    def test_control_non_object_json_gets_error_reply(self):
        _, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            await c.connect()
            replies = []
            for raw in ("[]", "null", '"str"', "7", '{"type":"nope"}'):
                await c.send_to(text_data=raw)
                replies.append(await c.receive_json_from())
            await c.disconnect()
            return replies

        replies = async_to_sync(run)()
        self.assertTrue(all(r["type"] == "error" for r in replies))

    def test_control_oversized_frame_not_stored(self):
        _, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            await c.connect()
            await c.send_to(
                text_data=json.dumps({"type": "chat_message", "content": "x" * 9000})
            )
            reply = await c.receive_json_from()
            await c.disconnect()
            return reply

        self.assertEqual(async_to_sync(run)()["type"], "error")
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_control_payload_validation_and_workspace_from_url_only(self):
        _, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            await c.connect()
            out = []
            for payload in (
                {"type": "chat_message", "content": 123},
                {"type": "chat_message", "content": "   "},
                {"type": "chat_message", "content": "y" * 2001},
                {"type": "chat_message", "content": "ok", "event_label": ["x"]},
                {"type": "chat_message", "content": "ok", "event_label": "z" * 201},
                {
                    "type": "chat_message",
                    "workspace_id": str(self.ws_b.id),
                    "content": "inject",
                },
            ):
                await c.send_json_to(payload)
                out.append(await c.receive_json_from())
            await c.disconnect()
            return out

        out = async_to_sync(run)()
        self.assertEqual([o["type"] for o in out[:5]], ["error"] * 5)
        self.assertEqual(out[5]["type"], "chat_message")
        self.assertFalse(ChatMessage.objects.filter(workspace=self.ws_b).exists())


@override_settings(RATELIMIT_ENABLED=True)
class RateLimitTests(WsBase):
    def test_control_burst_limit_is_per_user_across_sockets(self):
        _, cookie = login_client(self.kid_a)

        async def run():
            socks = [socket_for(self.ws_a, cookie) for _ in range(3)]
            for s in socks:
                await s.connect()
            for n in range(30):
                s = socks[n % 3]
                await s.send_json_to({"type": "chat_message", "content": f"m{n}"})
                await asyncio.sleep(0.05)
            await asyncio.sleep(2.5)  # let the consumers finish before counting
            for s in socks:
                await s.disconnect()

        async_to_sync(run)()
        self.assertEqual(ChatMessage.objects.filter(sender=self.kid_a).count(), 20)

    def test_poc_ws_06_oversized_frames_must_be_throttled(self):
        """
        WS-06: the 8192-char frame check runs BEFORE the rate limiter, so one
        session can send frames up to Daphne's 1 MiB cap forever and each one is
        answered. Assert the socket is throttled or closed within 200 frames.
        """
        _, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            await c.connect()
            blob = "x" * 9000
            replies = 0
            closed = False
            for _ in range(200):
                await c.send_to(text_data=blob)
                try:
                    msg = await c.receive_output(timeout=0.5)
                except asyncio.TimeoutError:
                    break
                if msg["type"] == "websocket.close":
                    closed = True
                    break
                replies += 1
            return replies, closed

        replies, closed = async_to_sync(run)()
        self.assertTrue(
            closed or replies < 200,
            f"server answered {replies}/200 oversized frames with no throttle or close",
        )


class FanoutCostTests(WsBase):
    """WS-07: per-message DB cost scales with the number of open sockets."""

    def test_poc_ws_07_broadcast_must_not_cost_db_per_socket(self):
        _, cp = login_client(self.parent_a)
        _, ck = login_client(self.kid_a)
        # Hardening spec 4.2 (Appendix B 10): one person may hold 5 sockets, so the
        # 10 listeners belong to two kids.
        kid2 = get_user_model().objects.create_user(
            username="wsa_kid2", password=PASSWORD
        )
        Membership.objects.create(
            user=kid2,
            workspace=self.ws_a,
            role=Membership.Role.KID,
            invited_by=self.parent_a,
            accepted_at=timezone.now(),
        )
        _, ck2 = login_client(kid2)

        async def run(n):
            sender = socket_for(self.ws_a, cp)
            await sender.connect()
            listeners = []
            for index in range(n):
                s = socket_for(self.ws_a, ck if index % 2 == 0 else ck2)
                await s.connect()
                listeners.append(s)
            with count_db_work() as counts:
                await sender.send_json_to(
                    {"type": "chat_message", "content": f"fan-{n}"}
                )
                for s in listeners:
                    await s.receive_json_from(timeout=5)
                await sender.receive_json_from(timeout=5)
            for s in listeners:
                await s.disconnect()
            await sender.disconnect()
            return dict(counts)

        base = async_to_sync(run)(1)
        many = async_to_sync(run)(10)
        self.assertLessEqual(
            many["queries"] - base["queries"],
            5,
            f"1 listener={base}, 10 listeners={many}",
        )


class HistoryPagingTests(WsBase):
    """?before= / ?after= on /chat/<uuid>/messages/ (the socket's catch-up path)."""

    def setUp(self):
        super().setUp()
        self.msgs_a = [
            ChatMessage.objects.create(
                workspace=self.ws_a, sender=self.parent_a, content=f"A{n}"
            )
            for n in range(5)
        ]
        self.msgs_b = [
            ChatMessage.objects.create(
                workspace=self.ws_b, sender=self.parent_b, content=f"B{n}"
            )
            for n in range(5)
        ]

    def url(self, ws):
        return f"/chat/{ws.id}/messages/"

    def test_control_cross_household_paging_is_404(self):
        client, _ = login_client(self.kid_a)
        for qs in (
            "",
            "?before=999999999",
            "?after=0",
            f"?after={self.msgs_b[0].id - 1}",
        ):
            with self.subTest(qs=qs):
                self.assertEqual(client.get(self.url(self.ws_b) + qs).status_code, 404)

    def test_control_ids_from_other_household_never_returned(self):
        client, _ = login_client(self.kid_a)
        # cursors that straddle household B's ids must still only return A's rows
        for qs in (
            f"?after={self.msgs_a[0].id - 1}",
            f"?before={self.msgs_b[-1].id + 1}",
            f"?after={self.msgs_b[0].id - 1}",
        ):
            with self.subTest(qs=qs):
                data = client.get(self.url(self.ws_a) + qs).json()
                self.assertTrue(
                    all(m["content"].startswith("A") for m in data["messages"])
                )

    def test_control_weird_cursors_never_500(self):
        client, _ = login_client(self.kid_a)
        for qs in (
            "?before=" + "9" * 30,
            "?after=" + "9" * 30,
            "?before=-5",
            "?after=-5",
            "?before=" + "9" * 5000,
            "?after=1e3",
            "?before=&after=",
            "?before=1&after=2",
            "?before=%00",
            "?before=0x10",
        ):
            with self.subTest(qs=qs[:40]):
                r = client.get(self.url(self.ws_a) + qs)
                self.assertIn(r.status_code, (200, 400), qs[:40])

    def test_control_login_required(self):
        r = Client().get(self.url(self.ws_a))
        self.assertEqual(r.status_code, 302)

    def test_control_paging_page_size_bounded(self):
        client, _ = login_client(self.kid_a)
        for n in range(150):
            ChatMessage.objects.create(
                workspace=self.ws_a, sender=self.parent_a, content=f"bulk{n}"
            )
        data = client.get(self.url(self.ws_a) + "?after=0").json()
        self.assertLessEqual(len(data["messages"]), 50)
        self.assertTrue(data["has_more"])


@override_settings(RATELIMIT_ENABLED=True)
class DbWorkBeforeLimiterTests(WsBase):
    """WS-08: receive_json runs has_access() (2 queries, new connection under
    CONN_MAX_AGE=0) BEFORE the rate limiter, so rejected frames still cost DB work."""

    def test_poc_ws_08_rate_limited_frames_must_not_hit_the_database(self):
        _, cookie = login_client(self.kid_a)

        async def run():
            c = socket_for(self.ws_a, cookie)
            await c.connect()
            # use up the burst allowance (20 per 10 s) with unsupported-type frames
            for _ in range(20):
                await c.send_json_to({"type": "nope"})
                await c.receive_json_from()
            with count_db_work() as counts:
                for _ in range(40):  # all 40 are over the limit
                    await c.send_json_to({"type": "nope"})
                    reply = await c.receive_json_from()
                    assert "too fast" in reply["message"], reply
            await c.disconnect()
            return counts["queries"]

        queries = async_to_sync(run)()
        self.assertEqual(
            queries, 0, f"{queries} SQL statements spent on 40 rate-limited frames"
        )


class SocketCapTests(WsBase):
    def test_poc_ws_10_sockets_per_user_must_be_capped(self):
        """WS-10: nothing limits concurrent sockets per user, session or IP."""
        _, cookie = login_client(self.kid_a)

        async def run():
            socks, accepted = [], 0
            for _ in range(40):
                c = socket_for(self.ws_a, cookie)
                ok, _ = await connect_and_check(c)
                if ok:
                    accepted += 1
                    socks.append(c)
            for c in socks:
                await c.disconnect()
            return accepted

        accepted = async_to_sync(run)()
        self.assertLessEqual(
            accepted, 10, f"{accepted}/40 sockets accepted for one session"
        )


class NginxEdgeLimitTests(TransactionTestCase):
    def test_poc_ws_09_websocket_path_needs_an_edge_connection_limit(self):
        """
        WS-09: .platform/hooks/postdeploy/03_https.sh puts limit_req only on login,
        signup and scan-ocr. /ws/ goes through `location /` with no limit_req or
        limit_conn, so unauthenticated handshakes (one DB query each when the
        cookie is junk) are unthrottled. Assert a limit applies to /ws/.
        """
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[3]
            / ".platform/hooks/postdeploy/03_https.sh"
        ).read_text()
        self.assertIn("limit_conn", script + "")  # a per-IP connection cap
        self.assertRegex(script, r"location\s+[^\n{]*ws[^\n{]*\{[^}]*limit_(req|conn)")
