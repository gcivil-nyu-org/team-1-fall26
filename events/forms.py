from datetime import datetime

from django import forms

from .models import Event


class EventForm(forms.Form):
    title = forms.CharField(max_length=200)
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    end_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    location = forms.CharField(label="Location (optional)", max_length=255, required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user if user is not None and user.is_authenticated else None
        self.event = None

    def clean(self):
        cleaned_data = super().clean()
        event_date = cleaned_data.get("date")
        if event_date is not None:
            datetime_field = forms.DateTimeField()
            for time_field, timestamp_field in (
                ("start_time", "starts_at"),
                ("end_time", "ends_at"),
            ):
                event_time = cleaned_data.get(time_field)
                if event_time is None:
                    continue
                try:
                    cleaned_data[timestamp_field] = datetime_field.clean(
                        datetime.combine(event_date, event_time)
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
