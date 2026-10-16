from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from django import forms
from django.utils import timezone

from .models import Event


class EventForm(forms.Form):
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
    timezone = forms.ChoiceField(required=False)
    location = forms.CharField(label="Location (optional)", max_length=255, required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user if user is not None and user.is_authenticated else None
        self.event = None
        self.fields["timezone"].choices = [(name, name) for name in sorted(available_timezones())]
        self.fields["timezone"].initial = timezone.get_current_timezone_name()

    def clean_timezone(self):
        return self.cleaned_data["timezone"] or timezone.get_current_timezone_name()

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
            title=cleaned_data["title"],
            starts_at=cleaned_data["starts_at"],
            ends_at=cleaned_data["ends_at"],
            location=cleaned_data["location"],
        )

        try:
            event.full_clean(
                exclude=["created_by"],
                validate_unique=False,
                validate_constraints=False,
            )
        except forms.ValidationError as error:
            field_names = {"starts_at": "start_time", "ends_at": "end_time"}
            for field, errors in error.error_dict.items():
                form_field = field_names.get(field, field)
                self.add_error(form_field if form_field in self.fields else None, errors)
        else:
            self.event = event

        return cleaned_data
