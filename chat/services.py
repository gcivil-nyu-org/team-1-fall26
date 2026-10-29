from django.core.exceptions import PermissionDenied, ValidationError

from accounts.models import Workspace
from .access import can_access_chat
from .models import ChatMessage

MAX_MESSAGE_LENGTH = 2000
MAX_EVENT_LABEL_LENGTH = 200


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


def create_message(*, sender, workspace_id, content, event_label=""):
    """Validate and save a message in an authorized workspace."""
    workspace = get_authorized_workspace(
        user=sender,
        workspace_id=workspace_id,
    )

    if not isinstance(content, str):
        raise ValidationError("Message must be text.")

    content = content.strip()

    if not content:
        raise ValidationError("Message cannot be empty.")

    if len(content) > MAX_MESSAGE_LENGTH:
        raise ValidationError("Message is too long.")

    if not isinstance(event_label, str):
        raise ValidationError("Event label must be text.")

    event_label = event_label.strip()

    if len(event_label) > MAX_EVENT_LABEL_LENGTH:
        raise ValidationError("Event label is too long.")

    return ChatMessage.objects.create(
        workspace=workspace,
        sender=sender,
        content=content,
        event_label=event_label,
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
