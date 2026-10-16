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

        return cleaned_data
