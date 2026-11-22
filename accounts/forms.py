import re
import unicodedata

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    UserCreationForm,
    UsernameField,
)
from django.core.mail import EmailMultiAlternatives
from django.core.validators import MaxLengthValidator
from django.template import loader

from syncmily import limits, ratelimit
from syncmily.safety import (
    RESERVED_USERNAME,
    RESERVED_USERNAME_MESSAGE,
    SafeFormMixin,
    is_reserved_username,
    username_validators,
)
from syncmily.text import clean_visible_text

from . import mail
from .models import Membership, Workspace
from .services import (
    ADDRESS_TAKEN_MESSAGE,
    email_ident,
    check_username,
    email_is_held,
)

__all__ = ["RESERVED_USERNAME", "RESERVED_USERNAME_MESSAGE"]


def check_username_not_reserved(username):
    """Only names a person picks go through this; the invite flow's own Parent2 and
    Kid3 are made by next_member_username and never reach a form.
    """
    if is_reserved_username(username):
        raise forms.ValidationError(RESERVED_USERNAME_MESSAGE)


# The shared normalizer (syncmily/text.py) under the name the forms already use. It
# turns line breaks into spaces and drops invisible format characters while keeping
# emoji joiners; nothing visible left gives an empty string.
clean_display_text = clean_visible_text


class WorkspaceForm(SafeFormMixin, forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name"]
        # The NAME policy refuses markup (unicode_000). URLs stay legal: the invite
        # email no longer carries the family name (spec 3.1, 3.3).
        text_policies = {"name": "NAME"}
        field_classes = {"name": forms.CharField}
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
        max_length=254,
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
        # One address, one account (spec 3.1, H-INVENTORY-08 and H-LOGIC-17).
        if email and email_is_held(email):
            raise forms.ValidationError(ADDRESS_TAKEN_MESSAGE)

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
        # UsernameField applies NFKC before clean_username, so the checks see the
        # name the model stores (H-LOGIC-10, H-MARKUP-046).
        field_classes = {"username": UsernameField}

    def clean_username(self):
        return check_username(self.cleaned_data["username"], user=self.instance)


class SignUpForm(UserCreationForm):
    """Sign-up checks only what the visitor typed. A taken username or an address
    that already has an account is not an error here: the view answers those like a
    fresh sign-up and emails the address owner instead (spec 10.2, finding
    signup-enumeration).
    """

    email = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "spellcheck": "false"}),
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")
        field_classes = {"username": UsernameField}

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if username is None:
            return username
        return username_validators(username)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def validate_unique(self):
        # Uniqueness is answered neutrally by the view, never on the page.
        pass


class SignInForm(AuthenticationForm):
    """The password sign-in. An over-long username or password is a normal failed
    sign-in, never a field error or a 500 (H-FUZZ-04)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        username = self.fields["username"]
        username.max_length = None
        username.validators = [
            check
            for check in username.validators
            if not isinstance(check, MaxLengthValidator)
        ]
        username.widget.attrs["maxlength"] = str(limits.LOGIN_USERNAME_MAX)
        username.widget.attrs["spellcheck"] = "false"

    def clean(self):
        username = self.cleaned_data.get("username") or ""
        password = self.cleaned_data.get("password") or ""
        if (
            len(username) > limits.LOGIN_USERNAME_MAX
            or len(password) > limits.PASSWORD_MAX
        ):
            raise self.get_invalid_login_error()
        return super().clean()


class FamilyHomeForm(SafeFormMixin, forms.Form):
    """Where "from home" trips start. The coordinates come from picking a suggestion."""

    class Meta:
        text_policies = {"location": "PLACE"}

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


class ResetRequestForm(PasswordResetForm):
    """Django only emails accounts that already have a password, so people who signed
    up with Google got no email and no error. Every active account with this address
    gets a link: Google sign-ups use it to set their first password.

    One address gets at most 3 reset emails an hour, whoever asks; past that the
    answer stays the same and no mail goes out (finding reset-mail-per-ip-cap). The
    mail leaves from a background thread, so a real account answers as fast as an
    unknown address (finding reset-timing-enumeration).
    """

    email = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []

    def get_users(self, email):
        rate = ratelimit.RATE_LIMITS["password_reset_recipient"]
        if not ratelimit.hit(
            "password_reset_recipient", email_ident(email), rate.limit, rate.window
        ):
            return iter(())
        email_field = get_user_model().get_email_field_name()
        users = get_user_model()._default_manager.filter(
            **{f"{email_field}__iexact": email, "is_active": True}
        )
        wanted = unicodedata.normalize("NFKC", email).casefold()
        return (
            user
            for user in users
            if unicodedata.normalize("NFKC", getattr(user, email_field)).casefold()
            == wanted
        )

    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        subject = "".join(
            loader.render_to_string(subject_template_name, context).splitlines()
        )
        body = loader.render_to_string(email_template_name, context)
        message = EmailMultiAlternatives(subject, body, from_email, [to_email])
        if html_email_template_name is not None:
            html = loader.render_to_string(html_email_template_name, context)
            message.attach_alternative(html, "text/html")
        self.sent.append(mail.send_in_background(message))


class TwoStepCodeForm(forms.Form):
    """A code from the authenticator app (6 to 8 digits) or a backup code (8 to 16
    letters and digits). Spaces are dropped; anything else counts as a wrong code."""

    code = forms.CharField(max_length=64, required=False, strip=True)

    CODE_PATTERN = re.compile(r"[0-9]{6,8}|[A-Za-z0-9]{8,16}")

    def clean_code(self):
        code = "".join((self.cleaned_data.get("code") or "").split())
        return code if self.CODE_PATTERN.fullmatch(code) else ""


class TwoStepEnrolForm(TwoStepCodeForm):
    """Turning two-step on needs the current password as well as a code, so a
    stolen session alone cannot enrol an attacker's app (finding
    factor-enrol-no-reauth)."""

    password = forms.CharField(
        strip=False, required=False, max_length=limits.PASSWORD_MAX
    )


class PaletteForm(forms.Form):
    palette = forms.ChoiceField(choices=Workspace.Palette.choices)
