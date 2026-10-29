from django.conf import settings
from django.db import models


class ChatMessage(models.Model):
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

    content = models.TextField(max_length=2000)
    event_label = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.sender}: {self.content[:50]}"
