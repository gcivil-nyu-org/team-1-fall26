from django import forms
from django.utils import timezone
from django.utils.dateformat import format as date_format

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
            Event.objects.filter(workspace=workspace).order_by("-starts_at", "pk")
            if workspace is not None
            else Event.objects.none()
        )
        self.fields["event"].empty_label = "No event"
        # "Soccer, Wed 28 Oct": several plans can share a title.
        self.fields["event"].label_from_instance = lambda event: (
            f"{event.title},"
            f" {date_format(timezone.localtime(event.starts_at), 'D j M Y')}"
        )

    def clean_title(self):
        title = self.cleaned_data["title"].strip()

        if not title:
            raise forms.ValidationError("Checklist title cannot be empty.")

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
            raise forms.ValidationError("Checklist item cannot be empty.")

        return description
