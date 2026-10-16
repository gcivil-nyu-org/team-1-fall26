from datetime import datetime

from django import forms


class EventForm(forms.Form):
    title = forms.CharField(max_length=200)
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    end_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    location = forms.CharField(label="Location (optional)", max_length=255, required=False)

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")

        if start_time is not None and end_time is not None and end_time <= start_time:
            self.add_error("end_time", "End time must be after start time.")

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

        return cleaned_data
