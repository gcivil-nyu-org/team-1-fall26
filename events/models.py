from datetime import datetime, timezone as datetime_timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def default_timezone_name():
    return settings.TIME_ZONE


class Event(models.Model):
    class Source(models.TextChoices):
        SYNCMILY = "syncmily", "Syncmily"
        GOOGLE = "google", "Google Calendar"

    class TransportMode(models.TextChoices):
        DRIVING = "driving", "Car"
        TRANSIT = "transit", "Subway / Bus"
        WALKING = "walking", "Walking"
        CYCLING = "cycling", "Cycling"

    submission_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_events",
    )
    workspace = models.ForeignKey(
        "accounts.Workspace",
        on_delete=models.PROTECT,
        related_name="events",
        null=True,
        blank=True,
    )
    requires_transport = models.BooleanField(default=False, db_default=False)
    dropoff_driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="dropoff_events",
        null=True,
        blank=True,
    )
    pickup_driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="pickup_events",
        null=True,
        blank=True,
    )
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="family_events",
        blank=True,
    )
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    timezone_name = models.CharField(max_length=64, default=default_timezone_name)
    location = models.CharField(
        max_length=255,
        blank=True,
    )
    latitude = models.FloatField(
        null=True,
        blank=True,
        help_text="Latitude of the event location.",
    )

    longitude = models.FloatField(
        null=True,
        blank=True,
        help_text="Longitude of the event location.",
    )
    transport_mode = models.CharField(
        max_length=20,
        choices=TransportMode.choices,
        default=TransportMode.TRANSIT,
    )
    # A plan that isn't settled yet (a maybe sleepover). Drawn with a dashed edge.
    is_tentative = models.BooleanField(default=False, db_default=False)
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.SYNCMILY,
        db_default=Source.SYNCMILY,
    )
    calendar_connection = models.ForeignKey(
        "calendars.CalendarConnection",
        on_delete=models.CASCADE,
        related_name="events",
        null=True,
        blank=True,
    )
    external_id = models.CharField(
        max_length=1024, blank=True, default="", db_default=""
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(dropoff_driver__isnull=True, pickup_driver__isnull=True)
                    | models.Q(requires_transport=True, workspace__isnull=False)
                ),
                name="event_drivers_require_family_transport",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="event_end_after_start",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source="syncmily",
                        calendar_connection__isnull=True,
                        external_id="",
                    )
                    | (
                        ~models.Q(source="syncmily")
                        & models.Q(calendar_connection__isnull=False)
                        & ~models.Q(external_id="")
                    )
                ),
                name="event_imported_fields_match_source",
            ),
            models.UniqueConstraint(
                fields=["calendar_connection", "external_id"],
                condition=models.Q(calendar_connection__isnull=False),
                name="unique_imported_event_per_connection",
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

        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            errors["timezone_name"] = "Choose a valid timezone for this event."

        for field in ("starts_at", "ends_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                errors[field] = "Choose a timezone for this event."

        if (
            isinstance(self.starts_at, datetime)
            and isinstance(self.ends_at, datetime)
            and "starts_at" not in errors
            and "ends_at" not in errors
            and self.ends_at.astimezone(datetime_timezone.utc)
            <= self.starts_at.astimezone(datetime_timezone.utc)
        ):
            errors["ends_at"] = "End time must be after start time."

        driver_ids = {self.dropoff_driver_id, self.pickup_driver_id} - {None}
        if driver_ids:
            from .access import event_drivers

            eligible_ids = set()
            if self.workspace_id:
                eligible_ids = set(
                    event_drivers(self.workspace)
                    .filter(pk__in=driver_ids)
                    .values_list("pk", flat=True)
                )
            for field in ("dropoff_driver", "pickup_driver"):
                driver_id = getattr(self, f"{field}_id")
                if driver_id and driver_id not in eligible_ids:
                    errors[field] = "Choose an active adult in this family."
                elif driver_id and not self.requires_transport:
                    errors[field] = (
                        "Mark transportation as needed before assigning a driver."
                    )

        if errors:
            raise ValidationError(errors)
