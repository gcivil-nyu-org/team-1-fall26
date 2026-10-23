"""Optional two-step sign-in: a code from an authenticator app (TOTP) after the password or Google.

Signing in is split in two when the account has two-step on. The first step (password or Google)
only records who is signing in, in the session; the user is logged in after the second step, a
6-digit code from their app or one of their single-use backup codes. django-otp keeps the secrets
and throttles wrong guesses per device.
"""
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
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

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


def _verify(user, code):
    """True if the code is a current app code or an unused backup code (which it then uses up)."""
    code = "".join(code.split())
    if not code:
        return False
    for device in TOTPDevice.objects.filter(user=user, confirmed=True):
        if device.verify_is_allowed()[0] and device.verify_token(code):
            return True
    for device in StaticDevice.objects.filter(user=user, confirmed=True):
        if device.verify_token(code.lower()):
            return True
    return False


def _pending_user(request):
    pending = request.session.get(PENDING_KEY)
    if not pending or time.time() - pending["started"] > PENDING_MAX_AGE:
        request.session.pop(PENDING_KEY, None)
        return None, None
    user = get_user_model().objects.filter(pk=pending["user_id"], is_active=True).first()
    return pending, user


@require_http_methods(["GET", "POST"])
def verify(request):
    pending, user = _pending_user(request)
    if user is None:
        messages.error(request, "Your sign-in timed out. Sign in again.")
        return redirect("accounts:login")

    error = ""
    if request.method == "POST":
        if _verify(user, request.POST.get("code", "")):
            request.session.pop(PENDING_KEY, None)
            login(request, user, backend=pending["backend"])
            next_url = pending.get("next") or ""
            if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
                next_url = reverse("accounts:dashboard")
            return redirect(next_url)
        pending["attempts"] += 1
        if pending["attempts"] >= MAX_ATTEMPTS:
            request.session.pop(PENDING_KEY, None)
            messages.error(request, "Too many wrong codes. Sign in again.")
            return redirect("accounts:login")
        request.session[PENDING_KEY] = pending
        error = "That code didn’t work. Check your app and try again, or use a backup code."

    return render(request, "accounts/two_step_verify.html", {"error": error})


def _qr_svg(url):
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buffer = BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()


def _new_backup_codes(user):
    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name="Backup codes", confirmed=True)
    codes = [StaticToken.random_token() for _ in range(BACKUP_CODE_COUNT)]
    StaticToken.objects.bulk_create(StaticToken(device=device, token=code) for code in codes)
    return codes


@login_required
@require_http_methods(["GET", "POST"])
def setup(request):
    if is_enabled(request.user):
        return redirect("accounts:profile")

    # One pending (unconfirmed) device per user: reuse it so the QR code stays the same on reload.
    device = TOTPDevice.objects.filter(user=request.user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=request.user, name="Authenticator app", confirmed=False)

    error = ""
    if request.method == "POST":
        code = "".join(request.POST.get("code", "").split())
        if code and device.verify_is_allowed()[0] and device.verify_token(code):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            codes = _new_backup_codes(request.user)
            return render(request, "accounts/two_step_backup_codes.html", {"codes": codes})
        error = "That code didn’t work. Make sure your phone’s time is set automatically, then try the newest code."

    secret = device.config_url.split("secret=")[1].split("&")[0]
    return render(
        request,
        "accounts/two_step_setup.html",
        {
            "qr_svg": _qr_svg(device.config_url),
            "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            "error": error,
        },
    )


@login_required
@require_POST
def disable(request):
    if _verify(request.user, request.POST.get("code", "")):
        TOTPDevice.objects.filter(user=request.user).delete()
        StaticDevice.objects.filter(user=request.user).delete()
        messages.success(request, "Two-step sign-in is off. You’ll sign in with just your password or Google.")
    else:
        messages.error(request, "That code didn’t work, so two-step sign-in is still on.")
    return redirect("accounts:profile")


@login_required
@require_POST
def new_backup_codes(request):
    if not is_enabled(request.user):
        return redirect("accounts:profile")
    if not _verify(request.user, request.POST.get("code", "")):
        messages.error(request, "That code didn’t work, so your backup codes are unchanged.")
        return redirect("accounts:profile")
    return render(request, "accounts/two_step_backup_codes.html", {"codes": _new_backup_codes(request.user)})


def backup_codes_left(user):
    return StaticToken.objects.filter(device__user=user, device__confirmed=True).count()
