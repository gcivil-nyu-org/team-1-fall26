
from django import forms


class AIEventSuggestionForm(forms.Form):
    """Accept a natural-language description of a family event."""

    description = forms.CharField(
        label="Describe your event",
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": (
                    "e.g. Take Emma to the dentist next Friday "
                    "at 3 PM at Brooklyn Dental Clinic"
                ),
                "autocomplete": "off",
            }
        ),
        help_text=(
            "Include the event, date, time, and location "
            "if you know them. You can edit everything "
            "before adding it to your calendar."
        ),
    )

    def clean_description(self):
        description = self.cleaned_data["description"].strip()

        if len(description) < 10:
            raise forms.ValidationError(
                "Please describe your event in a little more detail."
            )

        return description
