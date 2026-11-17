from django import forms
from django.utils import timezone
from django.utils.dateformat import format as date_format

from django.db.models import Q

from events.access import family_events
from events.models import Event

from .models import Checklist, ChecklistItem, clean_text


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

    def __init__(self, *args, workspace=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["event"].required = False

        # Only allow events from the current workspace.
        events = Event.objects.none()
        if workspace is not None:
            events = Event.objects.filter(workspace=workspace)
            if user is not None:
                # Only what this person may see; a Google import stays with its owner.
                events = events.filter(pk__in=family_events(user).values("pk")).exclude(
                    Q(source=Event.Source.GOOGLE) & ~Q(created_by=user)
                )
        self.fields["event"].queryset = events.order_by("-starts_at", "pk")
        self.fields["event"].empty_label = "No event"
        # "Soccer, Wed 28 Oct": several plans can share a title.
        self.fields["event"].label_from_instance = lambda event: (
            f"{event.title},"
            f" {date_format(timezone.localtime(event.starts_at), 'D j M Y')}"
        )

    def clean_title(self):
        title = clean_text(self.cleaned_data["title"])

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
        description = clean_text(self.cleaned_data["description"])

        if not description:
            raise forms.ValidationError("Checklist item cannot be empty.")

        return description
