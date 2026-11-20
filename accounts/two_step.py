"""Optional two-step sign-in: a code from an authenticator app (TOTP) after the password
or Google.

Signing in is split in two when the account has two-step on. The first step (password or
Google)
only records who is signing in, in the session; the user is logged in after the second
step, a
6-digit code from their app or one of their single-use backup codes. django-otp keeps
the secrets
and throttles wrong guesses per device.
"""

import logging
import time
from io import BytesIO

import qrcode
import qrcode.image.svg
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST
from django_otp import login as otp_login
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from syncmily import seclog
from syncmily.ratelimit import rate_limit

from .forms import TwoStepCodeForm, TwoStepEnrolForm
from .otp_storage import code_hashes, hash_code
from .services import end_user_sessions

PENDING_KEY = "two_step_pending"
PENDING_MAX_AGE = 300  # seconds to enter the code after the first step
MAX_ATTEMPTS = 5
BACKUP_CODE_COUNT = 10


def is_enabled(user):
    return TOTPDevice.objects.filter(user=user, confirmed=True).exists()


def begin(request, user, backend, next_url=""):
    """First step done: remember who is signing in and ask for their code."""
    request.session[PENDING_KEY] = {
        "user_id": user.pk,
        "backend": backend,
        "next": next_url,
        "started": time.time(),
        "attempts": 0,
    }
    return redirect("accounts:two_step_verify")


def _use_backup_code(device, code):
    """django-otp's StaticDevice.verify_token, but matching the stored hash of the
    code (accounts/otp_storage.py) instead of the code itself."""
    if not device.verify_is_allowed()[0]:
        return False
    match = device.token_set.filter(token__in=code_hashes(code)).first()
    if match is None:
        device.throttle_increment()
        return False
    match.delete()
    device.throttle_reset(commit=False)
    device.set_last_used_timestamp(commit=False)
    device.save()
    return True


def _verify(user, code):
    """The device that accepted the code (a current app code, or an unused backup
    code, which it then uses up), or None."""
    code = "".join((code or "").split())
    if not code:
        return None
    for device in TOTPDevice.objects.filter(user=user, confirmed=True):
        if device.verify_is_allowed()[0] and device.verify_token(code):
            return device
    for device in StaticDevice.objects.filter(user=user, confirmed=True):
        if _use_backup_code(device, code.lower()):
            return device
    return None


def _posted_code(request):
    form = TwoStepCodeForm(request.POST)
    return form.cleaned_data["code"] if form.is_valid() else ""


def _pending_user(request):
    pending = request.session.get(PENDING_KEY)
    if not pending or time.time() - pending["started"] > PENDING_MAX_AGE:
        request.session.pop(PENDING_KEY, None)
        return None, None
    user = (
        get_user_model().objects.filter(pk=pending["user_id"], is_active=True).first()
    )
    return pending, user


@require_http_methods(["GET", "POST"])
@rate_limit("two_step_verify")
def verify(request):
    pending, user = _pending_user(request)
    if user is None:
        messages.error(request, "Your sign-in timed out. Sign in again.")
        return redirect("accounts:login")

    error = ""
    if request.method == "POST":
        device = _verify(user, _posted_code(request))
        if device is not None:
            request.session.pop(PENDING_KEY, None)
            login(request, user, backend=pending["backend"])
            # Marks the session as verified for django-otp (the admin checks it).
            otp_login(request, device)
            if isinstance(device, StaticDevice):
                seclog.event(
                    "backup_code_used",
                    request=request,
                    user=user,
                    outcome="success",
                    codes_left=backup_codes_left(user),
                )
            next_url = pending.get("next") or ""
            if not url_has_allowed_host_and_scheme(
                next_url, {request.get_host()}, request.is_secure()
            ):
                next_url = reverse("accounts:dashboard")
            return redirect(next_url)
        pending["attempts"] += 1
        seclog.event(
            "two_step_code_failed",
            request=request,
            user=user,
            outcome="failure",
            attempts=pending["attempts"],
        )
        if pending["attempts"] >= MAX_ATTEMPTS:
            request.session.pop(PENDING_KEY, None)
            messages.error(request, "Too many wrong codes. Sign in again.")
            return redirect("accounts:login")
        request.session[PENDING_KEY] = pending
        error = (
            "That code didn’t work. Check your app and try again, or use a backup code."
        )

    return render(request, "accounts/two_step_verify.html", {"error": error})


def _qr_svg(url):
    image = qrcode.make(
        url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2
    )
    buffer = BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()


def _end_other_sessions(request):
    """Sign this user out everywhere else, so a session opened before two-step went on
    can't keep going without a code.
    """
    request.session.cycle_key()
    end_user_sessions(request.user, keep_session_key=request.session.session_key)


def _new_backup_codes(user):
    """Make 10 new backup codes and return them for showing once. Only their hashes
    are stored (finding otp-secrets-plaintext)."""
    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name="Backup codes", confirmed=True)
    codes = [StaticToken.random_token() for _ in range(BACKUP_CODE_COUNT)]
    StaticToken.objects.bulk_create(
        StaticToken(device=device, token=hash_code(code)) for code in codes
    )
    return codes


@login_required
@require_http_methods(["GET", "POST"])
@rate_limit("two_step_manage")
def setup(request):
    if is_enabled(request.user):
        return redirect("accounts:profile")

    # One pending (unconfirmed) device per user: reuse it so the QR code stays the same
    # on reload.
    device = TOTPDevice.objects.filter(user=request.user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(
            user=request.user, name="Authenticator app", confirmed=False
        )

    error = ""
    if request.method == "POST":
        form = TwoStepEnrolForm(request.POST)
        code = form.cleaned_data["code"] if form.is_valid() else ""
        password = form.cleaned_data.get("password", "") if form.is_valid() else ""
        if not (code and device.verify_is_allowed()[0] and device.verify_token(code)):
            error = (
                "That code didn’t work. Make sure your phone’s time is set"
                " automatically, then try the newest code."
            )
        elif not request.user.has_usable_password():
            error = (
                "Set a password on your profile first. Two-step sign-in asks for it"
                " before it turns on."
            )
        elif not request.user.check_password(password):
            # A live session alone must not enrol an authenticator (AUTH-13).
            error = "Enter your current password to turn on two-step sign-in."
        else:
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            seclog.event("two_step_enabled", request=request, outcome="success")
            _end_other_sessions(request)
            codes = _new_backup_codes(request.user)
            return render(
                request, "accounts/two_step_backup_codes.html", {"codes": codes}
            )

    secret = device.config_url.split("secret=")[1].split("&")[0]
    return render(
        request,
        "accounts/two_step_setup.html",
        {
            "qr_svg": _qr_svg(device.config_url),
            "secret": " ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
            "error": error,
        },
    )


@login_required
@require_POST
@rate_limit("two_step_manage")
def disable(request):
    if _verify(request.user, _posted_code(request)):
        TOTPDevice.objects.filter(user=request.user).delete()
        StaticDevice.objects.filter(user=request.user).delete()
        seclog.event("two_step_disabled", request=request, outcome="success")
        messages.success(
            request,
            "Two-step sign-in is off. You’ll sign in with just your password or"
            " Google.",
        )
    else:
        seclog.event(
            "two_step_disable_failed",
            request=request,
            outcome="failure",
            level=logging.WARNING,
        )
        messages.error(
            request, "That code didn’t work, so two-step sign-in is still on."
        )
    return redirect("accounts:profile")


@login_required
@require_POST
@rate_limit("two_step_manage")
def new_backup_codes(request):
    if not is_enabled(request.user):
        return redirect("accounts:profile")
    if not _verify(request.user, _posted_code(request)):
        messages.error(
            request, "That code didn’t work, so your backup codes are unchanged."
        )
        return redirect("accounts:profile")
    seclog.event("backup_codes_renewed", request=request, outcome="success")
    return render(
        request,
        "accounts/two_step_backup_codes.html",
        {"codes": _new_backup_codes(request.user)},
    )


def backup_codes_left(user):
    return StaticToken.objects.filter(device__user=user, device__confirmed=True).count()
