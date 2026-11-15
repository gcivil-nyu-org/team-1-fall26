import re
import unicodedata

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import Membership, Workspace
from syncmily.text import clean_visible_text

RESERVED_USERNAME = re.compile(
    r"^(admin|administrator|syncmily|support|parent\d*|kid\d*|caregiver\d*)$",
    re.IGNORECASE,
)
RESERVED_USERNAME_MESSAGE = (
    "That username is reserved. Choose another one; "
    "family members get names like Parent2 or Kid3 from their invite."
)


def check_username_not_reserved(username):
    """Only names a person picks go through this; the invite flow's own Parent2 and
    Kid3 are made by next_member_username and never reach a form.
    """
    if RESERVED_USERNAME.match(username):
        raise forms.ValidationError(RESERVED_USERNAME_MESSAGE)


def clean_display_text(value):
    """Turn line breaks and other whitespace into single spaces, then drop
    invisible format characters (bidi overrides, zero-width spaces) while
    keeping emoji joiners. Nothing visible left gives an empty string.
    """
    spaced = "".join(
        " " if char.isspace() or unicodedata.category(char) == "Cc" else char
        for char in value or ""
    )
    return " ".join(clean_visible_text(spaced).split())


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
        name = clean_display_text(self.cleaned_data["name"])

        if not name:
            raise forms.ValidationError("Workspace name cannot be empty.")
        if len(name) > Workspace._meta.get_field("name").max_length:
            raise forms.ValidationError("Workspace name is too long.")

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
        check_username_not_reserved(username)

        return username


class SignUpForm(UserCreationForm):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email", "spellcheck": "false"}),
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def clean_username(self):
        username = super().clean_username()
        if username:
            check_username_not_reserved(username.strip())
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        holders = get_user_model().objects.filter(email__iexact=email)

        if holders.filter(is_active=True).exists():
            raise forms.ValidationError(
                "An account with this email already exists. Sign in instead."
            )
        if holders.exists():
            # A pending invite holds this address; its link sets the password.
            raise forms.ValidationError(
                "This email has a pending invitation. Use the invite link "
                "you were emailed to join, or ask the parent to resend it."
            )

        return email
