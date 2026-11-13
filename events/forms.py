from copy import copy
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, available_timezones

from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.utils import timezone

from syncmily.text import clean_visible_text

from accounts.models import Workspace

from .access import event_drivers, event_participants, family_workspaces
from .conflicts import find_overlapping_events
from .models import Event

MIN_EVENT_DATE = date(1900, 1, 1)
MAX_EVENT_DATE = date(2100, 12, 31)

# The time-zone menu lists these, plus the current and chosen zones. The full list of
# about 600 zones was half of every page's HTML (the header date menu repeats it on
# every page); "Show all time zones" fetches it on demand. Any valid zone validates.
COMMON_TIMEZONES = frozenset(
    {
        "UTC",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Phoenix",
        "America/Los_Angeles",
        "America/Anchorage",
        "Pacific/Honolulu",
        "America/Toronto",
        "America/Vancouver",
        "America/Mexico_City",
        "America/Bogota",
        "America/Sao_Paulo",
        "America/Argentina/Buenos_Aires",
        "Europe/London",
        "Europe/Dublin",
        "Europe/Lisbon",
        "Europe/Paris",
        "Europe/Berlin",
        "Europe/Madrid",
        "Europe/Rome",
        "Europe/Amsterdam",
        "Europe/Athens",
        "Europe/Istanbul",
        "Africa/Lagos",
        "Africa/Cairo",
        "Africa/Johannesburg",
        "Asia/Dubai",
        "Asia/Kolkata",
        "Asia/Singapore",
        "Asia/Shanghai",
        "Asia/Hong_Kong",
        "Asia/Seoul",
        "Asia/Tokyo",
        "Australia/Sydney",
        "Pacific/Auckland",
    }
)


class TimezoneSelect(forms.Select):
    template_name = "events/widgets/timezone_select.html"

    def optgroups(self, name, value, attrs=None):
        keep = COMMON_TIMEZONES | {v for v in value if v}
        keep |= {timezone.get_current_timezone_name()}
        everything = self.choices
        self.choices = [c for c in everything if not c[0] or c[0] in keep]
        try:
            return super().optgroups(name, value, attrs)
        finally:
            self.choices = everything


def _clock_text(value):
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{value.hour % 12 or 12}:{value.minute:02d} {suffix}"


def _exists(naive, zone):
    """True unless the wall-clock time falls in a spring-forward gap."""
    round_trip = naive.replace(tzinfo=zone).astimezone(dt_timezone.utc).astimezone(zone)
    return round_trip.replace(tzinfo=None) == naive


def _wall_clock_to_aware(day, wall_time, zone):
    """
    Read a wall-clock time in a zone. An ambiguous time (clocks fall back) means
    its first occurrence; a time that does not exist (clocks spring forward) is an
    error that names the clock change.
    """
    naive = datetime.combine(day, wall_time)
    aware = naive.replace(tzinfo=zone)  # fold=0: the first occurrence
    if _exists(naive, zone):
        return aware
    later = naive
    for _ in range(24 * 60):
        later += timedelta(minutes=1)
        if _exists(later, zone):
            break
    raise forms.ValidationError(
        f"{_clock_text(naive)} doesn't exist on {day:%b} {day.day}, {day.year} "
        f"because clocks spring forward; pick {_clock_text(later)} or later."
    )


class EventTimezoneForm(forms.Form):
    timezone = forms.ChoiceField(required=False, widget=TimezoneSelect)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["timezone"].choices = [
            ("", f"Current timezone ({timezone.get_current_timezone_name()})"),
            *((name, name) for name in sorted(available_timezones())),
        ]
        self.fields["timezone"].initial = timezone.get_current_timezone_name()

    def clean_timezone(self):
        return self.cleaned_data["timezone"] or timezone.get_current_timezone_name()


class EventWorkspaceForm(EventTimezoneForm):
    workspace = forms.ModelChoiceField(queryset=Workspace.objects.none())

    date = forms.DateField(required=False, widget=forms.HiddenInput)

    # The day view's right-click "Add event at …" passes the slot's start time along.
    time = forms.TimeField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["workspace"].queryset = family_workspaces(
            user,
            caregivers_only=True,
        ).order_by("name", "pk")


class EventScheduleForm(EventTimezoneForm):
    date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    view = forms.ChoiceField(
        choices=[
            ("day", "Day"),
            ("week", "Week"),
            ("upcoming", "Upcoming"),
        ],
        required=False,
    )

    def clean(self):
        cleaned_data = super().clean()

        if self.errors:
            return cleaned_data

        selected_timezone = ZoneInfo(cleaned_data["timezone"])

        selected_date = cleaned_data.get("date") or timezone.localdate(
            timezone=selected_timezone
        )

        view = cleaned_data.get("view") or "day"

        if view == "upcoming":
            cleaned_data.update(
                date=selected_date,
                view=view,
                starts_at=timezone.now(),
            )
            return cleaned_data

        start_date = selected_date
        days = 1

        if view == "week":
            start_date -= timedelta(days=start_date.weekday())
            days = 7

        try:
            end_date = start_date + timedelta(days=days)
            previous_date = start_date - timedelta(days=days)
            next_date = end_date

            datetime_field = forms.DateTimeField()

            with timezone.override(selected_timezone):
                starts_at = datetime_field.clean(datetime.combine(start_date, time.min))
                ends_at = datetime_field.clean(datetime.combine(end_date, time.min))

        except (OverflowError, forms.ValidationError):
            self.add_error(
                "date",
                "Choose another date to display this schedule.",
            )
            return cleaned_data

        cleaned_data.update(
            date=selected_date,
            view=view,
            starts_at=starts_at,
            ends_at=ends_at,
            start_date=start_date,
            last_date=end_date - timedelta(days=1),
            previous_date=previous_date,
            next_date=next_date,
        )

        return cleaned_data


class EventForm(EventTimezoneForm):
    field_order = [
        "submission_token",
        "title",
        "date",
        "start_time",
        "end_date",
        "end_time",
        "timezone",
        "location",
        "latitude",
        "longitude",
        "transport_mode",
        "requires_transport",
        "dropoff_driver",
        "pickup_driver",
        "is_tentative",
        "participants",
    ]

    submission_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput,
    )

    title = forms.CharField(
        label="What are we planning?",
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Add an event title",
                "autocomplete": "off",
                "data-autofocus": "",
            }
        ),
    )

    date = forms.DateField(
        label="When?",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    start_time = forms.TimeField(
        widget=forms.TimeInput(attrs={"type": "time"}),
    )

    end_date = forms.DateField(
        label="End date (optional)",
        required=False,
        help_text="Leave blank if the event ends on the start date.",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    end_time = forms.TimeField(
        widget=forms.TimeInput(attrs={"type": "time"}),
    )

    # Location field used by Photon autocomplete.
    # Users can still enter a location manually.
    location = forms.CharField(
        label="Where (optional)",
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "Search for a place or address",
                "data-location-search": "",
            }
        ),
    )

    # Photon will populate these hidden coordinate fields
    # when a user selects a location suggestion.
    latitude = forms.FloatField(
        required=False,
        widget=forms.HiddenInput(),
    )

    longitude = forms.FloatField(
        required=False,
        widget=forms.HiddenInput(),
    )

    transport_mode = forms.ChoiceField(
        label="How will you travel to this event?",
        choices=Event.TransportMode.choices,
        initial=Event.TransportMode.TRANSIT,
    )

    is_tentative = forms.BooleanField(
        label="Not confirmed yet",
        required=False,
        help_text="It shows with a dashed edge until it’s settled.",
    )

    participants = forms.ModelMultipleChoiceField(
        label="Family members (optional)",
        queryset=get_user_model().objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Choose who is taking part. Everyone in the "
            "workspace can still view the event."
        ),
    )

    def clean_title(self):
        # Drop invisible format characters (zero-width spaces, bidi overrides).
        title = clean_visible_text(self.cleaned_data["title"])
        if not title:
            raise forms.ValidationError("Enter an event title.")
        return title

    def _clean_bounded_date(self, name):
        value = self.cleaned_data.get(name)
        if value is not None and not MIN_EVENT_DATE <= value <= MAX_EVENT_DATE:
            raise forms.ValidationError("Choose a date between 1900 and 2100.")
        return value

    def clean_date(self):
        return self._clean_bounded_date("date")

    def clean_end_date(self):
        return self._clean_bounded_date("end_date")

    def __init__(self, *args, user=None, workspace=None, instance=None, **kwargs):
        self.instance = instance
        if instance is not None:
            workspace = instance.workspace
            selected_timezone = ZoneInfo(instance.timezone_name)
            starts_at = timezone.localtime(instance.starts_at, selected_timezone)
            ends_at = timezone.localtime(instance.ends_at, selected_timezone)
            initial = {
                name: getattr(instance, name)
                for name in (
                    "title",
                    "location",
                    "latitude",
                    "longitude",
                    "transport_mode",
                    "is_tentative",
                    "requires_transport",
                )
            }
            initial.update(
                date=starts_at.date(),
                start_time=starts_at.time(),
                end_date=ends_at.date() if ends_at.date() != starts_at.date() else None,
                end_time=ends_at.time(),
                timezone=instance.timezone_name,
                participants=list(instance.participants.values_list("pk", flat=True)),
                dropoff_driver=instance.dropoff_driver_id,
                pickup_driver=instance.pickup_driver_id,
            )
            initial.update(kwargs.pop("initial", {}))
            kwargs["initial"] = initial
        super().__init__(*args, **kwargs)

        self.fields["timezone"].widget = forms.HiddenInput()

        self.user = user if user is not None and user.is_authenticated else None

        self.workspace = workspace

        if workspace is not None:
            driver_form = EventDriverForm(instance=Event(workspace=workspace))
            self.fields.update(driver_form.fields)
            self.order_fields(self.field_order)

        self.fields["participants"].queryset = event_participants(workspace)

        self.event = None

        if instance is None:
            self.fields["submission_token"].initial = signing.dumps(
                {
                    "id": str(uuid4()),
                    "user": self.user.pk if self.user else None,
                    "workspace": str(workspace.pk) if workspace else None,
                },
                salt="events.create",
            )
        else:
            self.fields.pop("submission_token")

    def clean(self):
        cleaned_data = super().clean()

        # Convert selected date and time into timezone-aware datetimes.
        event_date = cleaned_data.get("date")
        timezone_name = cleaned_data.get("timezone")

        if event_date is not None and timezone_name is not None:
            zone = ZoneInfo(timezone_name)
            end_day = cleaned_data.get("end_date")
            start_time = cleaned_data.get("start_time")
            end_time = cleaned_data.get("end_time")

            # An end time earlier than the start with no end date means the
            # event runs past midnight.
            if (
                end_day is None
                and start_time is not None
                and end_time is not None
                and end_time < start_time
            ):
                end_day = event_date + timedelta(days=1)

            for time_field, timestamp_field, day in (
                ("start_time", "starts_at", event_date),
                ("end_time", "ends_at", end_day or event_date),
            ):
                event_time = cleaned_data.get(time_field)

                if event_time is None:
                    continue

                try:
                    cleaned_data[timestamp_field] = _wall_clock_to_aware(
                        day, event_time, zone
                    )
                except forms.ValidationError as error:
                    self.add_error(time_field, error)
                except (OverflowError, ValueError):
                    self.add_error(
                        time_field,
                        "Choose a date between 1900 and 2100.",
                    )

        # Validate coordinates.
        latitude = cleaned_data.get("latitude")
        longitude = cleaned_data.get("longitude")
        location = cleaned_data.get("location", "").strip()

        if (latitude is None) != (longitude is None):
            self.add_error(
                "location",
                "Please select a location from the suggestions.",
            )

        if latitude is not None and not (-90 <= latitude <= 90):
            self.add_error(
                "latitude",
                "Invalid latitude.",
            )

        if longitude is not None and not (-180 <= longitude <= 180):
            self.add_error(
                "longitude",
                "Invalid longitude.",
            )

        # Coordinates must not be stored without a location.
        if not location and (latitude is not None or longitude is not None):
            self.add_error(
                "location",
                "A location is required when coordinates are provided.",
            )

        if self.errors:
            return cleaned_data

        requires_transport = cleaned_data.get("requires_transport", False)
        dropoff_driver = (
            cleaned_data.get("dropoff_driver") if requires_transport else None
        )
        pickup_driver = (
            cleaned_data.get("pickup_driver") if requires_transport else None
        )
        cleaned_data["dropoff_driver"] = dropoff_driver
        cleaned_data["pickup_driver"] = pickup_driver

        # Construct the event with all selected details.
        event = (
            copy(self.instance)
            if self.instance is not None
            else Event(created_by=self.user, workspace=self.workspace)
        )
        event_values = dict(
            title=cleaned_data["title"],
            timezone_name=cleaned_data["timezone"],
            starts_at=cleaned_data["starts_at"],
            ends_at=cleaned_data["ends_at"],
            location=location,
            latitude=latitude,
            longitude=longitude,
            transport_mode=cleaned_data["transport_mode"],
            is_tentative=cleaned_data.get("is_tentative", False),
            requires_transport=requires_transport,
            dropoff_driver=dropoff_driver,
            pickup_driver=pickup_driver,
        )
        for name, value in event_values.items():
            setattr(event, name, value)

        try:
            event.full_clean(
                exclude=["created_by", "workspace"],
                validate_unique=False,
                validate_constraints=False,
            )

        except forms.ValidationError as error:
            field_names = {
                "starts_at": "start_time",
                "ends_at": "end_time",
                "timezone_name": "timezone",
            }

            if hasattr(error, "error_dict"):
                for field, errors in error.error_dict.items():
                    form_field = field_names.get(field, field)

                    self.add_error(
                        form_field if form_field in self.fields else None,
                        errors,
                    )
            else:
                self.add_error(None, error)

        else:
            self.event = event

            # Check whether any selected family member
            # already has an overlapping event.
            participants = cleaned_data.get("participants")

            if self.workspace is not None and participants and not event.is_tentative:
                conflicts = find_overlapping_events(
                    workspace=self.workspace,
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    participants=participants,
                    exclude_event_id=event.pk,
                )

                conflict = conflicts.first()

                if conflict is not None:
                    conflicting_members = [
                        member.username
                        for member in participants
                        if conflict.participants.filter(pk=member.pk).exists()
                    ]

                    member_names = ", ".join(conflicting_members)

                    self.add_error(
                        None,
                        f"Scheduling conflict: {member_names} already "
                        f"has an event called '{conflict.title}' "
                        "during the selected time. "
                        "Please choose another time.",
                    )

        return cleaned_data


class EventDriverForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ["requires_transport", "dropoff_driver", "pickup_driver"]
        labels = {
            "requires_transport": "Transportation needed",
            "dropoff_driver": "Drop-off driver",
            "pickup_driver": "Pickup driver",
        }
        help_texts = {
            "requires_transport": (
                "Choose drivers for taking family members to and from this event."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        drivers = event_drivers(
            self.instance.workspace if self.instance.workspace_id else None
        )
        for field in ("dropoff_driver", "pickup_driver"):
            self.fields[field].queryset = drivers
            self.fields[field].empty_label = "Not assigned"
