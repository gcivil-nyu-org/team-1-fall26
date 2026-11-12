from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import Membership, Workspace


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": "The Reyes family",
                    "autocomplete": "off",
                }
            ),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if not name:
            raise forms.ValidationError("Workspace name cannot be empty.")

        return name


class InviteMemberForm(forms.Form):
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={"placeholder": "name@example.com"},
        ),
    )
    role = forms.ChoiceField(
        choices=Membership.Role.choices,
        widget=forms.RadioSelect,
        initial=Membership.Role.KID,
    )

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if (
            email
            and Membership.objects.filter(
                workspace=self.workspace,
                user__email__iexact=email,
            ).exists()
        ):
            raise forms.ValidationError(
                "Someone with this email is already in your workspace."
            )

        return email

    def clean(self):
        cleaned = super().clean()
        # Kids often have no email address; their parent shares the link.
        if (
            cleaned.get("role") == Membership.Role.PARENT
            and not cleaned.get("email")
            and "email" not in self.errors
        ):
            self.add_error("email", "Parents need an email address.")
        return cleaned


class UsernameChangeForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ["username"]

    def clean_username(self):
        username = self.cleaned_data["username"].strip()

        if (
            get_user_model()
            .objects.filter(username__iexact=username)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("That username is already taken.")

        return username


class SignUpForm(UserCreationForm):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email", "spellcheck": "false"}),
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account with this email already exists. Sign in instead."
            )

        return email


class FamilyHomeForm(forms.Form):
    """Where "from home" trips start. The coordinates come from picking a suggestion."""

    location = forms.CharField(
        label="Home address",
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Search your address",
                "autocomplete": "off",
                "data-location-search": "",
            }
        ),
    )
    latitude = forms.FloatField(
        widget=forms.HiddenInput, min_value=-90, max_value=90, required=False
    )
    longitude = forms.FloatField(
        widget=forms.HiddenInput, min_value=-180, max_value=180, required=False
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("latitude") is None or cleaned.get("longitude") is None:
            raise forms.ValidationError(
                "Pick your address from the suggestions so trips know where home is."
            )
        return cleaned
