
from django import forms

from events.models import Event

from .models import Checklist, ChecklistItem


class ChecklistForm(forms.ModelForm):
    class Meta:
        model = Checklist
        fields = ["title", "event"]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "placeholder": "e.g. Soccer Practice Essentials",
                }
            ),
        }

    def __init__(self, *args, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["event"].required = False

        # Only allow events from the current workspace.
        self.fields["event"].queryset = (
            Event.objects.filter(workspace=workspace)
            if workspace is not None
            else Event.objects.none()
        )

    def clean_title(self):
        title = self.cleaned_data["title"].strip()

        if not title:
            raise forms.ValidationError(
                "Checklist title cannot be empty."
            )

        return title


class ChecklistItemForm(forms.ModelForm):
    class Meta:
        model = ChecklistItem
        fields = ["description"]
        widgets = {
            "description": forms.TextInput(
                attrs={
                    "placeholder": "e.g. Pack water bottle",
                }
            ),
        }

    def clean_description(self):
        description = self.cleaned_data["description"].strip()

        if not description:
            raise forms.ValidationError(
                "Checklist item cannot be empty."
            )

        return description
