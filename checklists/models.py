import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Checklist(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    workspace = models.ForeignKey(
        "accounts.Workspace",
        on_delete=models.CASCADE,
        related_name="checklists",
    )

    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checklists",
    )

    title = models.CharField(max_length=150)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_checklists",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()

        self.title = self.title.strip()
        errors = {}

        if not self.title:
            errors["title"] = "Checklist title cannot be empty."

        if self.event_id and self.workspace_id:
            from events.models import Event

            if not Event.objects.filter(
                pk=self.event_id,
                workspace_id=self.workspace_id,
            ).exists():
                errors["event"] = "The event must belong to this workspace."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.title


class ChecklistItem(models.Model):
    checklist = models.ForeignKey(
        Checklist,
        on_delete=models.CASCADE,
        related_name="items",
    )

    description = models.CharField(max_length=255)

    is_completed = models.BooleanField(default=False)

    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_checklist_items",
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    position = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.description
