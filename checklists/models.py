import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from syncmily.locking import VersionedModel
from syncmily import constraints
from syncmily.safety import CleanCharField, CleanTextQuerySet
from syncmily.text import clean_visible_text

# The shared normalizer, under the name the checklist forms already use.
clean_text = clean_visible_text


class Checklist(VersionedModel):
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

    title = CleanCharField(max_length=150, policy="TITLE")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_checklists",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CleanTextQuerySet.as_manager()

    class Meta:
        constraints = [
            constraints.not_blank("checklist", "title"),
            constraints.clean_text("checklist", "title"),
        ]

    def clean(self):
        super().clean()

        raw_title = self.title
        self.title = clean_text(raw_title)
        errors = {}

        # A blank title already failed the field check; don't report it twice.
        if raw_title and not self.title:
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


class ChecklistItem(VersionedModel):
    checklist = models.ForeignKey(
        Checklist,
        on_delete=models.CASCADE,
        related_name="items",
    )

    description = CleanCharField(max_length=255, policy="TITLE")

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

    objects = CleanTextQuerySet.as_manager()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            constraints.not_blank("checklistitem", "description"),
            constraints.clean_text("checklistitem", "description"),
        ]

    def __str__(self):
        return self.description
