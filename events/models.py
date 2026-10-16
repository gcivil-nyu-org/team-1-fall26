from datetime import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Event(models.Model):
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_events",
    )
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    location = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="event_end_after_start",
            ),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        self.title = self.title.strip()
        errors = {}

        if not self.title:
            errors["title"] = "Enter an event title."

        for field in ("starts_at", "ends_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                errors[field] = "Choose a timezone for this event."

        if (
            isinstance(self.starts_at, datetime)
            and isinstance(self.ends_at, datetime)
            and "starts_at" not in errors
            and "ends_at" not in errors
            and self.ends_at <= self.starts_at
        ):
            errors["ends_at"] = "End time must be after start time."

        if errors:
            raise ValidationError(errors)
