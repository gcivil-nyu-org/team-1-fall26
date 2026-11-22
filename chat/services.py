from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from accounts.models import Workspace
from syncmily import limits
from syncmily.safety import POLICIES, enforce_quota, validate_text
from syncmily.text import has_visible, is_malformed

from .access import can_access_chat
from .models import ChatMessage, sender_name_for

MAX_MESSAGE_LENGTH = limits.TEXT.chat_message
MAX_EVENT_LABEL_LENGTH = limits.TEXT.chat_event_label


def get_authorized_workspace(*, user, workspace_id):
    """Return the workspace only when the user has chat access."""
    if not getattr(user, "is_authenticated", False):
        raise PermissionDenied("Authentication is required.")

    if not user.is_active:
        raise PermissionDenied("Your account is inactive.")

    try:
        workspace = Workspace.objects.get(pk=workspace_id)
    except (Workspace.DoesNotExist, ValueError, TypeError):
        raise PermissionDenied("Workspace not found or access denied.")

    if not can_access_chat(user, workspace):
        raise PermissionDenied("You do not have access to this workspace.")

    return workspace


def create_message(
    *,
    sender,
    workspace_id,
    content,
    event_label="",
    priority=ChatMessage.Priority.NORMAL,
):
    """Validate and save a message in an authorized workspace.

    The text goes through the CHAT policy (spec 3.1): multiline, mark floods trimmed,
    markup, NUL and lone surrogates refused, and nothing visible counts as empty.
    Then the family and per-person quotas run, and the row is stored, all in one
    transaction. `event_label` from clients is ignored and stored as "" (spec 9.7).
    """
    workspace = get_authorized_workspace(
        user=sender,
        workspace_id=workspace_id,
    )

    if not isinstance(content, str):
        raise ValidationError("Message must be text.")

    content = validate_text(content, POLICIES["CHAT"])

    # The label is never stored, but a client that sends one must send text.
    if event_label is not None:
        if not isinstance(event_label, str):
            raise ValidationError("Event label must be text.")
        if len(event_label) > MAX_EVENT_LABEL_LENGTH:
            raise ValidationError("Event label is too long.")
        if is_malformed(event_label):
            raise ValidationError("Event label has characters that are not allowed.")

    # Hangul fillers and other blanks survive normalizing but draw nothing.
    if not has_visible(content):
        raise ValidationError("Message cannot be empty.")

    if not isinstance(priority, str) or priority not in ChatMessage.Priority.values:
        priority = ChatMessage.Priority.NORMAL

    with transaction.atomic():
        enforce_quota(workspace, "chat_messages")
        enforce_quota(workspace, "chat_messages_per_user_day", user=sender)
        return ChatMessage.objects.create(
            workspace=workspace,
            sender=sender,
            sender_name=sender_name_for(sender),
            content=content,
            event_label="",
            priority=priority,
        )


def get_household_messages(*, user, workspace_id, limit=50):
    """Retrieve recent messages only for authorized members."""
    workspace = get_authorized_workspace(
        user=user,
        workspace_id=workspace_id,
    )

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValidationError("Limit must be between 1 and 100.")

    messages = (
        ChatMessage.objects.filter(workspace=workspace)
        .select_related("sender")
        .order_by("-created_at", "-id")[:limit]
    )

    return list(reversed(list(messages)))


def serialize_message(message):
    return {
        "id": message.id,
        "sender_id": message.sender_id,
        # The name stored at send time; rows written before the snapshot existed
        # (or in bulk) fall back to the current username.
        "sender": message.sender_name or message.sender.get_username(),
        "content": message.content,
        "event_label": message.event_label,
        "priority": message.priority,
        "created_at": message.created_at.isoformat(),
    }


def get_message_page(*, workspace, before_id=None, after_id=None, limit=50):
    """
    Up to `limit` messages older than `before_id` or newer than `after_id`,
    oldest first, plus whether more remain in that direction.
    """
    messages = ChatMessage.objects.filter(workspace=workspace).select_related("sender")
    if after_id is not None:
        page = list(messages.filter(id__gt=after_id).order_by("id")[: limit + 1])
        has_more = len(page) > limit
        return page[:limit], has_more
    if before_id is not None:
        messages = messages.filter(id__lt=before_id)
    page = list(messages.order_by("-id")[: limit + 1])
    has_more = len(page) > limit
    return list(reversed(page[:limit])), has_more


def purge_old_messages(now=None):
    """Nightly chat retention (spec 10.1, chat storage).

    Deletes messages older than 12 months, then trims any family that holds more
    than the 50,000-message cap down to its newest 45,000. Returns how many rows went.
    """
    now = now or timezone.now()
    quotas = limits.QUOTAS
    cutoff = now - timedelta(days=quotas.chat_retention_days)
    deleted, _ = ChatMessage.objects.filter(created_at__lt=cutoff).delete()

    crowded = (
        ChatMessage.objects.values("workspace_id")
        .annotate(total=Count("id"))
        .filter(total__gt=quotas.chat_messages_per_family)
        .values_list("workspace_id", flat=True)
    )
    for workspace_id in list(crowded):
        family = ChatMessage.objects.filter(workspace_id=workspace_id)
        keep = family.order_by("-created_at", "-id").values("id")[: quotas.chat_trim_to]
        trimmed, _ = family.exclude(id__in=keep).delete()
        deleted += trimmed
    return deleted
