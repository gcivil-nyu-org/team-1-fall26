from django import forms
from django.contrib.auth import get_user_model

from .models import Membership, Workspace


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": "Enter your family workspace name",
                    "class": "form-control",
                }
            ),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if not name:
            raise forms.ValidationError(
                "Workspace name cannot be empty."
            )

        return name

class InviteMemberForm(forms.Form):
    email = forms.EmailField(
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

        if Membership.objects.filter(
            workspace=self.workspace,
            user__email__iexact=email,
        ).exists():
            raise forms.ValidationError(
                "Someone with this email is already in your workspace."
            )

        return email


class UsernameChangeForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ["username"]

    def clean_username(self):
        username = self.cleaned_data["username"].strip()

        if (
            get_user_model().objects
            .filter(username__iexact=username)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("That username is already taken.")

        return username
