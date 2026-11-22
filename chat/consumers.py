"""
The household chat WebSocket (hardening spec section 6, with the 10.1 changes).

Each frame is counted against the per-person limits before anything else happens, so
a refused frame costs no decoding and no database work. Every malformed frame gets an
error frame and the socket stays open, until one socket sends 5 bad frames in a
minute. Each message is authorized again, for the sender and for every receiver,
with two queries however many sockets are open. Socket caps live in an in-process
registry (chat/registry.py).

Close codes: 4400 too many bad frames, 4403 no access (or access ended), 4429 too
many sockets or too many refused frames.
"""

import asyncio
import json
import logging
import time
from collections import deque

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.exceptions import PermissionDenied, ValidationError

from syncmily import limits, seclog
from syncmily.ratelimit import RATE_LIMITS, hit
from syncmily.text import is_malformed

from . import registry
from .access import authorized_sockets
from .services import (
    create_message,
    get_authorized_workspace,
    serialize_message,
)

logger = logging.getLogger(__name__)

MAX_FRAME_CHARS = limits.WEBSOCKET_FRAME_MAX_CHARS
MAX_DEPTH = 3
BAD_FRAMES_BEFORE_CLOSE = 5
BAD_FRAME_WINDOW_SECONDS = 60
FRAME_SCOPES = ("chat_burst", "chat_hour", "chat_day")

CLOSE_BAD_FRAMES = 4400
CLOSE_FORBIDDEN = 4403
CLOSE_TOO_MANY = 4429

TOO_FAST = "You're sending messages too fast. Wait a moment."
TEXT_ONLY = "Only text messages are supported."
TOO_LONG = "Message is too long."
INVALID = "Invalid message format."
UNSUPPORTED = "Unsupported message type."
NOT_ALLOWED = "This message has characters that are not allowed."
TRY_AGAIN = "Could not send. Try again."


def _limited(scope, ident):
    """Count one hit for a chat scope in process memory. True while under the limit."""
    rate = RATE_LIMITS[scope]
    return hit(scope, ident, rate.limit, rate.window)


def _too_deep(value, depth=0):
    if depth > MAX_DEPTH:
        return True
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return False
    return any(_too_deep(child, depth + 1) for child in children)


def _has_malformed(value):
    """True when any string in a decoded frame holds NUL or a lone surrogate."""
    if isinstance(value, str):
        return is_malformed(value)
    if isinstance(value, dict):
        return any(_has_malformed(k) or _has_malformed(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_malformed(item) for item in value)
    return False


def frame_problem(payload):
    """The error to send for a decoded frame, or None when its shape is right.

    The frame must be an object of type chat_message whose content is text. Nesting
    deeper than 3 and any NUL or lone surrogate refuse the whole frame. The service
    checks the rest: an unknown priority is stored as normal, and an event label must
    be short text but is never stored (spec 9.7).
    """
    if not isinstance(payload, dict):
        return INVALID
    if _too_deep(payload):
        return INVALID
    if _has_malformed(payload):
        return NOT_ALLOWED
    if payload.get("type") != "chat_message":
        return UNSUPPORTED
    if not isinstance(payload.get("content"), str):
        return "Message must be text."
    return None


class HouseholdChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.closing = False
        self.socket = None
        self.bad_frames = deque()
        self.user = self.scope.get("user")
        self.user_id = getattr(self.user, "pk", None)
        self.workspace_id = self.scope["url_route"]["kwargs"]["workspace_id"]
        self.group_name = f"chat_{self.workspace_id.hex}"

        if not getattr(self.user, "is_authenticated", False):
            self.log_close(CLOSE_FORBIDDEN, "not_signed_in")
            await self.close(code=CLOSE_FORBIDDEN)
            return

        if not _limited("ws_connect", self.user_id):
            self.log_close(CLOSE_TOO_MANY, "connect_rate")
            # Accept first, so the browser sees the close code, not a bare 403.
            await self.accept()
            await self.close(code=CLOSE_TOO_MANY)
            return

        if not await self.has_access():
            self.log_close(CLOSE_FORBIDDEN, "no_access")
            # Accept first, so the browser sees the close code, not a bare 403.
            await self.accept()
            await self.close(code=CLOSE_FORBIDDEN)
            return

        # Check the caps and add this socket in one step: no await in between.
        self.socket = registry.claim(
            self.channel_name,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            session_key=self._session_key(),
            loop=asyncio.get_running_loop(),
            revoke=self.revoke,
        )
        if self.socket is None:
            self.log_close(CLOSE_TOO_MANY, "socket_cap")
            # Accept first, so the browser sees the close code, not a bare 403.
            await self.accept()
            await self.close(code=CLOSE_TOO_MANY)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    def _session_key(self):
        session = self.scope.get("session")
        if session is None:
            return None
        try:
            return session.session_key
        except Exception:  # noqa: BLE001  (no usable session: nothing to re-check)
            return None

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
    def save_message(self, content, priority, event_label, sockets):
        """Re-check who may read the chat, then store the message.

        Returns the serialized message and the channel names allowed to receive it.
        Raises PermissionDenied when the sender itself has lost access.
        """
        allowed, users = authorized_sockets(self.workspace_id, sockets)
        if self.channel_name not in allowed:
            raise PermissionDenied("Chat access ended.")
        message = create_message(
            sender=users[self.user_id],
            workspace_id=self.workspace_id,
            content=content,
            event_label=event_label,
            priority=priority,
        )
        return serialize_message(message), sorted(allowed)

    async def send_json(self, content):
        await self.send(text_data=json.dumps(content))

    async def send_error(self, message):
        await self.send_json({"type": "error", "message": message})

    def log_close(self, code, reason):
        """One security line for a refusal close: who, which family, and why."""
        client = self.scope.get("client") or ("-",)
        seclog.event(
            "websocket_refused",
            user=self.user if self.user_id is not None else None,
            outcome=f"closed_{code}",
            level=logging.WARNING if code != CLOSE_BAD_FRAMES else logging.INFO,
            code=code,
            reason=reason,
            workspace_id=self.workspace_id,
            client_ip=client[0],
            route=self.scope.get("path"),
        )

    async def end(self, code, reason="closed"):
        """Close from the server side and free this socket's slot at once."""
        if self.closing:
            return
        self.closing = True
        self.log_close(code, reason)
        registry.release(self.channel_name)
        if self.socket is not None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        await self.close(code=code)

    async def revoke(self):
        """Close at once: the person was removed from the family or signed out."""
        await self.end(CLOSE_FORBIDDEN, "revoked")

    async def bad_frame(self, message):
        """Answer a malformed frame. 5 within a minute on one socket close it."""
        await self.send_error(message)
        now = time.monotonic()
        self.bad_frames.append(now)
        while self.bad_frames and now - self.bad_frames[0] > BAD_FRAME_WINDOW_SECONDS:
            self.bad_frames.popleft()
        if len(self.bad_frames) >= BAD_FRAMES_BEFORE_CLOSE:
            await self.end(CLOSE_BAD_FRAMES, "bad_frames")

    async def websocket_receive(self, message):
        text = message.get("text")
        if text is not None:
            await self.receive(text_data=text)
        else:
            await self.receive(bytes_data=message.get("bytes") or b"")

    async def receive(self, text_data=None, bytes_data=None):
        if self.closing:
            return

        # 1. Count the frame before reading it. A refused frame does no other work.
        if not all(_limited(scope, self.user_id) for scope in FRAME_SCOPES):
            await self.send_error(TOO_FAST)
            if not _limited("chat_refused", self.user_id):
                await self.end(CLOSE_TOO_MANY, "frame_rate")
            return

        # 2-4. Binary, oversized and undecodable frames.
        if text_data is None:
            await self.bad_frame(TEXT_ONLY)
            return
        if len(text_data) > MAX_FRAME_CHARS:
            await self.bad_frame(TOO_LONG)
            return
        try:
            payload = json.loads(text_data)
        except (ValueError, RecursionError, UnicodeError):
            await self.bad_frame(INVALID)
            return

        # 5. Shape.
        problem = frame_problem(payload)
        if problem is not None:
            await self.send_error(problem)
            return

        # 6-7. Re-check access for everyone and store.
        sockets = registry.family_sockets(self.workspace_id)
        try:
            message, allowed = await self.save_message(
                payload["content"],
                payload.get("priority"),
                payload.get("event_label"),
                sockets,
            )
        except PermissionDenied:
            await self.end(CLOSE_FORBIDDEN, "access_ended")
            return
        except ValidationError as exc:
            await self.send_error("; ".join(exc.messages))
            return
        except Exception as exc:  # noqa: BLE001  (keep the socket; log the type only)
            logger.error(
                "chat message not saved user=%s workspace=%s error=%s",
                self.user_id,
                self.workspace_id,
                type(exc).__name__,
            )
            await self.send_error(TRY_AGAIN)
            return

        checked = max((socket.serial for socket in sockets), default=0)
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat.message",
                "message": message,
                "allowed": allowed,
                "checked": checked,
            },
        )

    async def chat_message(self, event):
        if self.closing:
            return
        if self.channel_name in event.get("allowed", ()):
            await self.send_json({"type": "chat_message", **event["message"]})
            return
        if self.socket is not None and self.socket.serial > event.get("checked", 0):
            # This socket opened after the sender's check; it loads the message from
            # history instead.
            return
        await self.end(CLOSE_FORBIDDEN, "access_ended")

    async def disconnect(self, close_code):
        self.closing = True
        registry.release(self.channel_name)
        if getattr(self, "socket", None) is not None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
