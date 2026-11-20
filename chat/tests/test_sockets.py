"""Socket caps, revocation, close codes, sender names and retention (spec 6, 10.1)."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import Membership
from accounts.services import remove_member
from syncmily import limits
from syncmily.asgi import application

from .. import registry
from ..models import ChatMessage
from ..services import create_message, purge_old_messages, serialize_message
from .test_core import make_household

PASSWORD = "test-password-123"


def socket(workspace, user=None, cookie=None):
    headers = [(b"host", b"localhost"), (b"origin", b"http://localhost:8000")]
    if cookie:
        headers.append((b"cookie", f"sessionid={cookie}".encode()))
    communicator = WebsocketCommunicator(
        application, f"/ws/chat/{workspace.id}/", headers=headers
    )
    if user is not None:
        communicator.scope["user"] = user
    return communicator


def session_cookie(user):
    client = Client()
    assert client.login(username=user.get_username(), password=PASSWORD)
    return client, client.cookies["sessionid"].value


async def close_code(communicator, timeout=1.0):
    """The close code the server sent, or None when the socket stayed open."""
    while True:
        try:
            message = await asyncio.wait_for(communicator.output_queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        if message["type"] == "websocket.close":
            return message.get("code")


async def in_thread(function, *args):
    """Run sync code on a worker thread and close that thread's DB connection after,
    so the test database can be dropped at the end of the run."""

    def call():
        try:
            return function(*args)
        finally:
            connection.close()

    return await asyncio.get_running_loop().run_in_executor(None, call)


class RegistryTests(TestCase):
    def tearDown(self):
        for name in [f"c{n}" for n in range(40)]:
            registry.release(name)

    def test_caps_per_user_and_per_family(self):
        claimed = [
            registry.claim(f"c{n}", user_id=1, workspace_id="fam") for n in range(6)
        ]
        self.assertEqual(sum(socket is not None for socket in claimed), 5)
        family = [
            registry.claim(f"c{n}", user_id=100 + n, workspace_id="fam")
            for n in range(6, 40)
        ]
        self.assertEqual(
            5 + sum(socket is not None for socket in family),
            limits.QUOTAS.chat_sockets_per_family,
        )

    def test_release_is_idempotent_and_frees_the_slot(self):
        for n in range(5):
            registry.claim(f"c{n}", user_id=1, workspace_id="fam")
        registry.release("c0")
        registry.release("c0")
        self.assertIsNotNone(registry.claim("c5", user_id=1, workspace_id="fam"))
        self.assertEqual(len(registry.user_sockets(1)), 5)


class RevocationTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.workspace, self.admin, self.kid = make_household("rv_admin", "rv_kid")

    def test_removing_a_member_closes_their_socket_at_once(self):
        membership = Membership.objects.get(user=self.kid)

        async def run():
            kid = socket(self.workspace, self.kid)
            self.assertTrue((await kid.connect())[0])
            await in_thread(
                lambda: remove_member(
                    workspace=self.workspace,
                    removed_by=self.admin,
                    membership_id=membership.pk,
                )
            )
            return await close_code(kid)

        self.assertEqual(async_to_sync(run)(), 4403)

    def test_signing_out_closes_only_that_sessions_sockets(self):
        laptop_client, laptop = session_cookie(self.kid)
        _, phone = session_cookie(self.kid)

        async def run():
            first = socket(self.workspace, cookie=laptop)
            second = socket(self.workspace, cookie=phone)
            self.assertTrue((await first.connect())[0])
            self.assertTrue((await second.connect())[0])
            await in_thread(laptop_client.logout)
            codes = await close_code(first), await close_code(second, 0.3)
            await second.disconnect()
            return codes

        self.assertEqual(async_to_sync(run)(), (4403, None))


class CloseCodeTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.workspace, self.admin, self.kid = make_household("cc_admin", "cc_kid")

    def test_five_bad_frames_close_with_4400(self):
        async def run():
            communicator = socket(self.workspace, self.kid)
            await communicator.connect()
            replies = []
            for _ in range(5):
                await communicator.send_to(text_data="{not json")
                replies.append(await communicator.receive_json_from())
            return replies, await close_code(communicator)

        replies, code = async_to_sync(run)()
        self.assertEqual({r["message"] for r in replies}, {"Invalid message format."})
        self.assertEqual(code, 4400)

    @override_settings(RATELIMIT_ENABLED=True)
    def test_refused_frames_past_fifty_close_with_4429(self):
        async def run():
            communicator = socket(self.workspace, self.kid)
            await communicator.connect()
            frame = json.dumps({"type": "nope"})
            for _ in range(20):
                await communicator.send_to(text_data=frame)
                await communicator.receive_json_from()
            # 50 refused frames a minute get an error each; the next one closes.
            for _ in range(51):
                await communicator.send_to(text_data=frame)
            return await close_code(communicator, 2.0)

        self.assertEqual(async_to_sync(run)(), 4429)

    def test_an_unexpected_error_keeps_the_socket(self):
        async def run():
            communicator = socket(self.workspace, self.kid)
            await communicator.connect()
            with mock.patch(
                "chat.consumers.create_message", side_effect=RuntimeError("boom")
            ):
                await communicator.send_json_to(
                    {"type": "chat_message", "content": "hi"}
                )
                reply = await communicator.receive_json_from()
            await communicator.send_json_to({"type": "chat_message", "content": "ok"})
            after = await communicator.receive_json_from()
            await communicator.disconnect()
            return reply, after

        reply, after = async_to_sync(run)()
        self.assertEqual(
            reply, {"type": "error", "message": "Could not send. Try again."}
        )
        self.assertEqual(after["content"], "ok")

    def test_event_label_is_ignored(self):
        async def run():
            communicator = socket(self.workspace, self.kid)
            await communicator.connect()
            await communicator.send_json_to(
                {"type": "chat_message", "content": "hi", "event_label": "Soccer"}
            )
            reply = await communicator.receive_json_from()
            await communicator.disconnect()
            return reply

        self.assertEqual(async_to_sync(run)()["event_label"], "")
        self.assertEqual(ChatMessage.objects.get().event_label, "")


class SenderNameAndRetentionTests(TestCase):
    def setUp(self):
        self.workspace, self.admin, self.kid = make_household("sn_admin", "sn_kid")

    def test_messages_keep_the_name_they_were_sent_under(self):
        message = create_message(
            sender=self.kid, workspace_id=self.workspace.pk, content="hello"
        )
        self.kid.username = "renamed_kid"
        self.kid.save()
        message.refresh_from_db()
        self.assertEqual(message.sender_name, "sn_kid")
        self.assertEqual(serialize_message(message)["sender"], "sn_kid")

    def test_markup_and_blank_messages_are_refused(self):
        from django.core.exceptions import ValidationError

        for content in ("<img src=x onerror=alert(1)>", "ㅤㅤ", "a\x00b"):
            with self.subTest(content=content), self.assertRaises(ValidationError):
                create_message(
                    sender=self.kid, workspace_id=self.workspace.pk, content=content
                )
        message = create_message(
            sender=self.kid, workspace_id=self.workspace.pk, content="We <3 soccer"
        )
        self.assertEqual(message.content, "We <3 soccer")

    def test_purge_drops_old_messages_and_trims_crowded_families(self):
        now = timezone.now()
        old = ChatMessage.objects.create(
            workspace=self.workspace, sender=self.kid, content="old"
        )
        ChatMessage.objects.filter(pk=old.pk).update(
            created_at=now - timedelta(days=400)
        )
        recent = [
            ChatMessage.objects.create(
                workspace=self.workspace, sender=self.kid, content=f"m{n}"
            )
            for n in range(6)
        ]
        small = replace(limits.QUOTAS, chat_messages_per_family=5, chat_trim_to=3)
        with mock.patch.object(limits, "QUOTAS", small):
            deleted = purge_old_messages(now)
        self.assertEqual(deleted, 4)
        self.assertEqual(
            list(ChatMessage.objects.values_list("pk", flat=True).order_by("pk")),
            [message.pk for message in recent[-3:]],
        )
