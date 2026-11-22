from django.conf import settings
from django.db import models

from syncmily import constraints, limits
from syncmily.safety import CleanCharField, CleanTextField, CleanTextQuerySet
from syncmily.text import truncate_graphemes

SENDER_NAME_LENGTH = limits.TEXT.chat_sender_name


def sender_name_for(user):
    """The name a message keeps for its sender: the username at send time."""
    return truncate_graphemes(user.get_username(), SENDER_NAME_LENGTH)


class ChatMessage(models.Model):
    class Priority(models.TextChoices):
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    workspace = models.ForeignKey(
        "accounts.Workspace",
        on_delete=models.CASCADE,
        related_name="chat_messages",
        null=True,
        blank=True,
    )

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_messages",
    )

    # The sender's username when the message was sent, so a rename never relabels
    # old messages (H-LOGIC-16).
    sender_name = models.CharField(
        max_length=SENDER_NAME_LENGTH, blank=True, default="", db_default=""
    )

    content = CleanTextField(max_length=2000, policy="CHAT")
    priority = models.CharField(
        max_length=6,
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_default=Priority.NORMAL,
    )
    event_label = CleanCharField(max_length=200, blank=True, policy="LABEL")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CleanTextQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            constraints.length_between(
                "chatmessage", "content", 1, limits.TEXT.chat_message
            ),
            constraints.clean_text("chatmessage", "content", multiline=True),
            constraints.clean_text("chatmessage", "event_label"),
        ]

    def save(self, *args, **kwargs):
        if not self.sender_name and self.sender_id:
            self.sender_name = sender_name_for(self.sender)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sender}: {self.content[:50]}"
