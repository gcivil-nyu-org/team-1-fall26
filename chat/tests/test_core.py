from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import Membership, Workspace
from syncmily.asgi import application

from ..services import create_message, get_household_messages


def make_household(admin_name, member_name=None):
    """A workspace with its primary admin and, optionally, one accepted member."""
    User = get_user_model()
    admin = User.objects.create_user(username=admin_name, password="test-password-123")
    workspace = Workspace.objects.create(
        name=f"{admin_name} family", primary_admin=admin
    )
    member = None
    if member_name:
        member = User.objects.create_user(
            username=member_name, password="test-password-123"
        )
        Membership.objects.create(
            user=member,
            workspace=workspace,
            role=Membership.Role.KID,
            invited_by=admin,
            accepted_at=timezone.now(),
        )
    return workspace, admin, member


class ChatServiceTests(TestCase):
    def setUp(self):
        self.workspace, self.user, self.member = make_household(
            "service_admin", "service_kid"
        )
        self.other_workspace, self.outsider, _ = make_household("other_admin")

    def test_create_message(self):
        message = create_message(
            sender=self.user,
            workspace_id=self.workspace.id,
            content="  Dinner is ready!  ",
        )

        self.assertEqual(message.content, "Dinner is ready!")
        self.assertEqual(message.workspace_id, self.workspace.id)

    def test_accepted_member_can_post(self):
        message = create_message(
            sender=self.member,
            workspace_id=self.workspace.id,
            content="Can I sleep over on Friday?",
        )

        self.assertEqual(message.sender, self.member)

    def test_reject_empty_message(self):
        with self.assertRaises(ValidationError):
            create_message(
                sender=self.user,
                workspace_id=self.workspace.id,
                content="   ",
            )

    def test_reject_missing_household(self):
        with self.assertRaises(PermissionDenied):
            create_message(
                sender=self.user,
                workspace_id=None,
                content="Hello",
            )

    def test_outsider_cannot_post_or_read(self):
        with self.assertRaises(PermissionDenied):
            create_message(
                sender=self.outsider,
                workspace_id=self.workspace.id,
                content="Let me in",
            )
        with self.assertRaises(PermissionDenied):
            get_household_messages(user=self.outsider, workspace_id=self.workspace.id)

    def test_household_history_is_isolated(self):
        create_message(
            sender=self.user,
            workspace_id=self.workspace.id,
            content="Household one",
        )

        create_message(
            sender=self.outsider,
            workspace_id=self.other_workspace.id,
            content="Household two",
        )

        messages = get_household_messages(
            user=self.user,
            workspace_id=self.workspace.id,
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "Household one")


class HouseholdChatWebSocketTests(TransactionTestCase):
    """The socket runs in other threads, so the data must be committed for them to see
    it.
    """

    def setUp(self):
        self.workspace, self.admin, self.member = make_household(
            "socket_admin", "socket_kid"
        )
        _, self.outsider, _ = make_household("socket_outsider")

    def connect(self, user):
        async def run():
            communicator = WebsocketCommunicator(
                application,
                f"/ws/chat/{self.workspace.id}/",
                headers=[
                    (b"host", b"localhost"),
                    (b"origin", b"http://localhost:8000"),
                ],
            )
            if user is not None:
                communicator.scope["user"] = user
            connected, code = await communicator.connect()
            await communicator.disconnect()
            return connected, code

        return async_to_sync(run)()

    def test_anonymous_connection_rejected(self):
        connected, _ = self.connect(None)

        self.assertFalse(connected)

    def test_outsider_connection_rejected(self):
        connected, code = self.connect(self.outsider)

        self.assertFalse(connected)
        self.assertEqual(code, 4403)

    def test_member_receives_messages_with_sender_name(self):
        async def run():
            communicator = WebsocketCommunicator(
                application,
                f"/ws/chat/{self.workspace.id}/",
                headers=[
                    (b"host", b"localhost"),
                    (b"origin", b"http://localhost:8000"),
                ],
            )
            communicator.scope["user"] = self.member
            connected, _ = await communicator.connect()
            await communicator.send_json_to(
                {"type": "chat_message", "content": "Hi family"}
            )
            reply = await communicator.receive_json_from()
            await communicator.disconnect()
            return connected, reply

        connected, reply = async_to_sync(run)()

        self.assertTrue(connected)
        self.assertEqual(reply["content"], "Hi family")
        self.assertEqual(reply["sender"], "socket_kid")
