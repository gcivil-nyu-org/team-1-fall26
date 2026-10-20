from datetime import datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, available_timezones

from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.utils import timezone

from accounts.models import Workspace

from .access import event_participants, family_workspaces
from .models import Event


class EventTimezoneForm(forms.Form):
    timezone = forms.ChoiceField(required=False)

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

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["workspace"].queryset = family_workspaces(user, caregivers_only=True).order_by("name", "pk")


class EventScheduleForm(EventTimezoneForm):
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    view = forms.ChoiceField(choices=[("day", "Day"), ("week", "Week"), ("upcoming", "Upcoming")], required=False)

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data

        selected_timezone = ZoneInfo(cleaned_data["timezone"])
        selected_date = cleaned_data.get("date") or timezone.localdate(timezone=selected_timezone)
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
            self.add_error("date", "Choose another date to display this schedule.")
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
        "submission_token", "title", "date", "start_time", "end_date", "end_time", "timezone", "location", "participants",
    ]
    submission_token = forms.CharField(required=False, widget=forms.HiddenInput)
    title = forms.CharField(max_length=200)
    date = forms.DateField(label="Start date", widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    end_date = forms.DateField(
        label="End date (optional)",
        required=False,
        help_text="Leave blank if the event ends on the start date.",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    location = forms.CharField(label="Location (optional)", max_length=255, required=False)

    participants = forms.ModelMultipleChoiceField(
        label="Family members (optional)",
        queryset=get_user_model().objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Choose who is taking part. Everyone in the workspace can still view the event.",
    )

    def __init__(self, *args, user=None, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user if user is not None and user.is_authenticated else None
        self.workspace = workspace
        self.fields["participants"].queryset = event_participants(workspace)
        self.event = None
        self.fields["submission_token"].initial = signing.dumps(
            {
                "id": str(uuid4()),
                "user": self.user.pk if self.user else None,
                "workspace": str(workspace.pk) if workspace else None,
            },
            salt="events.create",
        )

    def clean(self):
        cleaned_data = super().clean()
        event_date = cleaned_data.get("date")
        timezone_name = cleaned_data.get("timezone")
        if event_date is not None and timezone_name is not None:
            datetime_field = forms.DateTimeField()
            for time_field, timestamp_field, day in (
                ("start_time", "starts_at", event_date),
                ("end_time", "ends_at", cleaned_data.get("end_date") or event_date),
            ):
                event_time = cleaned_data.get(time_field)
                if event_time is None:
                    continue
                try:
                    with timezone.override(ZoneInfo(timezone_name)):
                        cleaned_data[timestamp_field] = datetime_field.clean(
                            datetime.combine(day, event_time)
                        )
                except forms.ValidationError as error:
                    self.add_error(time_field, error)

        if self.errors:
            return cleaned_data

        event = Event(
            created_by=self.user,
            workspace=self.workspace,
            title=cleaned_data["title"],
            timezone_name=cleaned_data["timezone"],
            starts_at=cleaned_data["starts_at"],
            ends_at=cleaned_data["ends_at"],
            location=cleaned_data["location"],
        )

        try:
            event.full_clean(
                exclude=["created_by", "workspace"],
                validate_unique=False,
                validate_constraints=False,
            )
        except forms.ValidationError as error:
            field_names = {"starts_at": "start_time", "ends_at": "end_time", "timezone_name": "timezone"}
            for field, errors in error.error_dict.items():
                form_field = field_names.get(field, field)
                self.add_error(form_field if form_field in self.fields else None, errors)
        else:
            self.event = event

        return cleaned_data
