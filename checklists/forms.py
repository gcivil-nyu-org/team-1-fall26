from uuid import UUID, uuid4

from django import forms
from django.core import signing
from django.core.validators import MaxLengthValidator
from django.db.models import Q
from django.utils.dateformat import format as date_format

from events.access import family_events
from events.models import Event
from syncmily.localtime import localtime_or_utc
from syncmily.safety import SafeCharField, SafeFormMixin
from syncmily.text import normalize_text

from .models import Checklist, ChecklistItem

SUBMISSION_SALT = "checklists.create"
SUBMISSION_MAX_AGE = 86400


def read_submission(token, user, workspace):
    """The checklist id a signed create token carries, or None when the token is
    missing, forged, expired or made for another person or family."""
    try:
        data = signing.loads(token, salt=SUBMISSION_SALT, max_age=SUBMISSION_MAX_AGE)
        if data["user"] != user.pk or data["workspace"] != str(workspace.pk):
            return None
        return UUID(data["id"])
    except (signing.BadSignature, KeyError, TypeError, ValueError, AttributeError):
        return None


class ListTextField(SafeCharField):
    """A text field that names a too-long value in Django's usual words ("at most 150
    characters") before the markup check can answer first, as a long run of letters
    looks like encoded data."""

    def to_python(self, value):
        if isinstance(value, str) and len(value) > self.max_length:
            clean = normalize_text(value)
            if len(clean) > self.max_length:
                MaxLengthValidator(self.max_length)(clean)
        return super().to_python(value)


class ChecklistForm(SafeFormMixin, forms.ModelForm):
    # A signed token a create page carries, so a double click or a retried post makes
    # one list (the token's id becomes the list's id). Edits carry the version instead.
    submission_token = forms.CharField(required=False, widget=forms.HiddenInput)
    version = forms.IntegerField(
        required=False, min_value=0, max_value=2**31 - 1, widget=forms.HiddenInput
    )

    class Meta:
        model = Checklist
        fields = ["title", "event"]
        field_classes = {"title": ListTextField}
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
            f" {date_format(localtime_or_utc(event.starts_at), 'D j M Y')}"
        )

        if self.instance.pk and not self.instance._state.adding:
            self.fields.pop("submission_token")
            self.initial["version"] = self.instance.version
        else:
            self.fields.pop("version")
            if user is not None and workspace is not None:
                self.fields["submission_token"].initial = signing.dumps(
                    {
                        "id": str(uuid4()),
                        "user": user.pk,
                        "workspace": str(workspace.pk),
                    },
                    salt=SUBMISSION_SALT,
                )


class ChecklistItemForm(SafeFormMixin, forms.ModelForm):
    version = forms.IntegerField(
        required=False, min_value=0, max_value=2**31 - 1, widget=forms.HiddenInput
    )

    class Meta:
        model = ChecklistItem
        fields = ["description"]
        field_classes = {"description": ListTextField}
        widgets = {
            "description": forms.TextInput(
                attrs={
                    "placeholder": "e.g. Pack water bottle",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.instance._state.adding:
            self.initial["version"] = self.instance.version
        else:
            self.fields.pop("version")
