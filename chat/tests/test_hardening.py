from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from syncmily.asgi import application

from ..models import ChatMessage
from .test_core import make_household


def open_socket(workspace, user):
    communicator = WebsocketCommunicator(
        application,
        f"/ws/chat/{workspace.id}/",
        headers=[
            (b"host", b"localhost"),
            (b"origin", b"http://localhost:8000"),
        ],
    )
    communicator.scope["user"] = user
    return communicator


@override_settings(RATELIMIT_ENABLED=True)
class ChatFloodTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.workspace, self.admin, self.kid = make_household("flood_a", "flood_k")

    def test_burst_over_limit_is_refused_and_not_saved(self):
        async def run():
            communicator = open_socket(self.workspace, self.kid)
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            replies = []
            for n in range(25):
                await communicator.send_json_to(
                    {"type": "chat_message", "content": f"m{n}"}
                )
                replies.append(await communicator.receive_json_from())
            await communicator.disconnect()
            return replies

        replies = async_to_sync(run)()

        self.assertEqual([r["type"] for r in replies[:20]], ["chat_message"] * 20)
        self.assertEqual(replies[20]["type"], "error")
        self.assertIn("too fast", replies[20]["message"])
        self.assertEqual(ChatMessage.objects.count(), 20)

    def test_oversized_frame_is_dropped_before_decoding(self):
        async def run():
            communicator = open_socket(self.workspace, self.kid)
            await communicator.connect()
            await communicator.send_to(text_data="x" * 20000)
            reply = await communicator.receive_json_from()
            await communicator.disconnect()
            return reply

        reply = async_to_sync(run)()

        self.assertEqual(reply["type"], "error")
        self.assertEqual(ChatMessage.objects.count(), 0)


class ChatHistoryEndpointTests(TestCase):
    def setUp(self):
        self.workspace, self.admin, self.kid = make_household("hist_a", "hist_k")
        self.other, self.outsider, _ = make_household("hist_o")
        self.ids = [
            ChatMessage.objects.create(
                workspace=self.workspace, sender=self.admin, content=f"m{n}"
            ).id
            for n in range(120)
        ]
        self.url = reverse("chat:messages", args=[self.workspace.id])

    def test_before_returns_older_page(self):
        self.client.force_login(self.kid)

        data = self.client.get(self.url, {"before": self.ids[70]}).json()

        self.assertEqual(len(data["messages"]), 50)
        self.assertEqual(data["messages"][0]["content"], "m20")
        self.assertEqual(data["messages"][-1]["content"], "m69")
        self.assertTrue(data["has_more"])

    def test_before_reports_end_of_history(self):
        self.client.force_login(self.kid)

        data = self.client.get(self.url, {"before": self.ids[5]}).json()

        self.assertEqual(len(data["messages"]), 5)
        self.assertFalse(data["has_more"])

    def test_after_returns_newer_messages_in_order(self):
        self.client.force_login(self.kid)

        data = self.client.get(self.url, {"after": self.ids[117]}).json()

        self.assertEqual([m["content"] for m in data["messages"]], ["m118", "m119"])

    def test_other_household_gets_404(self):
        self.client.force_login(self.outsider)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_login_required(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_bad_cursor_is_400(self):
        self.client.force_login(self.kid)

        self.assertEqual(self.client.get(self.url, {"before": "abc"}).status_code, 400)

    def test_page_offers_older_messages(self):
        self.client.force_login(self.kid)

        page = self.client.get(reverse("chat:workspace_chat", args=[self.workspace.id]))

        self.assertContains(page, 'id="chat-older"')
