from django.conf import settings
from django.db import models

from .fields import EncryptedTextField


class CalendarConnection(models.Model):
    class Provider(models.TextChoices):
        GOOGLE = "google", "Google Calendar"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="calendar_connections",
    )
    workspace = models.ForeignKey(
        "accounts.Workspace",
        on_delete=models.CASCADE,
        related_name="calendar_connections",
        null=True,
        blank=True,
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    account_email = models.EmailField(blank=True)
    calendar_id = models.CharField(max_length=255, default="primary")
    # OAuth tokens are credentials: they're encrypted at rest, and must never be
    # logged or shown in templates.
    access_token = EncryptedTextField(blank=True)
    refresh_token = EncryptedTextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True)
    sync_token = models.TextField(blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"],
                name="one_calendar_connection_per_provider",
            ),
        ]

    def __str__(self):
        account = self.account_email or self.user
        return f"{self.get_provider_display()} ({account})"
