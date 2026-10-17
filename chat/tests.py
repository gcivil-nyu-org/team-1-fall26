
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from channels.testing import WebsocketCommunicator

from syncmily.asgi import application

from .models import ChatMessage
from .services import create_message, get_household_messages
from .services import create_message, get_household_messages


class ChatServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="service_test_user",
            password="test-password-123",
        )

    def test_create_message(self):
        message = create_message(
            sender=self.user,
            household_id=1,
            content="  Dinner is ready!  ",
        )

        self.assertEqual(message.content, "Dinner is ready!")
        self.assertEqual(message.household_id, 1)

    def test_reject_empty_message(self):
        with self.assertRaises(ValidationError):
            create_message(
                sender=self.user,
                household_id=1,
                content="   ",
            )

    def test_reject_missing_household(self):
        with self.assertRaises(ValidationError):
            create_message(
                sender=self.user,
                household_id=None,
                content="Hello",
            )

    def test_household_history_is_isolated(self):
        create_message(
            sender=self.user,
            household_id=1,
            content="Household one",
        )

        create_message(
            sender=self.user,
            household_id=2,
            content="Household two",
        )

        messages = get_household_messages(
            household_id=1,
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "Household one")

from django.test import SimpleTestCase
from channels.testing import WebsocketCommunicator
from syncmily.asgi import application


class HouseholdChatWebSocketTests(SimpleTestCase):
    async def test_connection_rejected_until_authorization_is_ready(self):
        communicator = WebsocketCommunicator(
            application,
            "/ws/chat/",
            headers=[
                (b"host", b"localhost"),
                (b"origin", b"http://localhost:8000"),
            ],
        )

        connected, _ = await communicator.connect()

        self.assertFalse(connected)
        await communicator.disconnect()
