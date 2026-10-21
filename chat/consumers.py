
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.core.exceptions import PermissionDenied, ValidationError

from .services import create_message, get_authorized_workspace


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
    def save_message(self, content, event_label):
        message = create_message(
            sender=self.user,
            workspace_id=self.workspace_id,
            content=content,
            event_label=event_label,
        )

        return {
            "id": message.id,
            "sender_id": message.sender_id,
            "sender": message.sender.get_username(),
            "content": message.content,
            "event_label": message.event_label,
            "created_at": message.created_at.isoformat(),
        }

    async def receive_json(self, content, **kwargs):
        if not await self.has_access():
            await self.close(code=4403)
            return

        if not isinstance(content, dict):
            await self.send_json({
                "type": "error",
                "message": "Invalid message format.",
            })
            return

        if content.get("type") != "chat_message":
            await self.send_json({
                "type": "error",
                "message": "Unsupported message type.",
            })
            return

        try:
            message = await self.save_message(
                content.get("content"),
                content.get("event_label", ""),
            )
        except ValidationError as exc:
            await self.send_json({
                "type": "error",
                "message": "; ".join(exc.messages),
            })
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

        await self.send_json({
            "type": "chat_message",
            **event["message"],
        })

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )
