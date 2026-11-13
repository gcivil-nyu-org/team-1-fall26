from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.core.exceptions import PermissionDenied, ValidationError

from syncmily.ratelimit import hit

from .services import (
    create_message,
    get_authorized_workspace,
    serialize_message,
)

MAX_FRAME_CHARS = 8192


class HouseholdChatConsumer(AsyncJsonWebsocketConsumer):

    async def connect(self):
        self.user = self.scope.get("user")
        self.workspace_id = self.scope["url_route"]["kwargs"]["workspace_id"]
        self.group_name = f"chat_{self.workspace_id.hex}"

        if not await self.has_access():
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name,
        )

        await self.accept()

    @database_sync_to_async
    def has_access(self):
        try:
            get_authorized_workspace(
                user=self.user,
                workspace_id=self.workspace_id,
            )
            return True
        except PermissionDenied:
            return False

    @database_sync_to_async
    def save_message(self, content, event_label, priority):
        message = create_message(
            sender=self.user,
            workspace_id=self.workspace_id,
            content=content,
            event_label=event_label,
            priority=priority,
        )

        return serialize_message(message)

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        # Drop oversized frames before they are decoded.
        if text_data is not None and len(text_data) > MAX_FRAME_CHARS:
            await self.send_json({"type": "error", "message": "Message is too long."})
            return
        await super().receive(text_data=text_data, bytes_data=bytes_data, **kwargs)

    async def receive_json(self, content, **kwargs):
        if not await self.has_access():
            await self.close(code=4403)
            return

        ident = self.user.pk
        if not (
            hit("chat_burst", ident, 20, 10) and hit("chat_hour", ident, 300, 3600)
        ):
            await self.send_json(
                {
                    "type": "error",
                    "message": "You're sending messages too fast. Wait a moment.",
                }
            )
            return

        if not isinstance(content, dict):
            await self.send_json(
                {
                    "type": "error",
                    "message": "Invalid message format.",
                }
            )
            return

        if content.get("type") != "chat_message":
            await self.send_json(
                {
                    "type": "error",
                    "message": "Unsupported message type.",
                }
            )
            return

        try:
            message = await self.save_message(
                content.get("content"),
                content.get("event_label", ""),
                content.get("priority"),
            )
        except ValidationError as exc:
            await self.send_json(
                {
                    "type": "error",
                    "message": "; ".join(exc.messages),
                }
            )
            return
        except PermissionDenied:
            await self.close(code=4403)
            return

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat.message",
                "message": message,
            },
        )

    async def chat_message(self, event):
        if not await self.has_access():
            await self.close(code=4403)
            return

        await self.send_json(
            {
                "type": "chat_message",
                **event["message"],
            }
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )
