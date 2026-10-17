
from django.core.exceptions import ValidationError

from .models import ChatMessage


MAX_MESSAGE_LENGTH = 2000
MAX_EVENT_LABEL_LENGTH = 200


def create_message(*, sender, household_id, content, event_label=""):
    if not sender.is_authenticated or not sender.pk:
        raise ValidationError("Authentication is required.")

    if not isinstance(household_id, int) or isinstance(household_id, bool) or household_id <= 0:
        raise ValidationError("A valid household ID is required.")

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
        sender=sender,
        household_id=household_id,
        content=content,
        event_label=event_label,
    )


def get_household_messages(*, household_id, limit=50):
    if not isinstance(household_id, int) or isinstance(household_id, bool) or household_id <= 0:
        raise ValidationError("A valid household ID is required.")

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValidationError("Limit must be between 1 and 100.")

    messages = (
        ChatMessage.objects
        .filter(household_id=household_id)
        .select_related("sender")
        .order_by("-created_at", "-id")[:limit]
    )

    return list(reversed(list(messages)))

