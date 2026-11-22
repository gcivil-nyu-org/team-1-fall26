"""Signal receivers for the accounts app (connected in AccountsConfig.ready)."""

import logging

from syncmily import seclog
from syncmily.safety import clean_for_column

PERSON_NAME_FIELDS = ("first_name", "last_name")


def normalize_person_names(sender, instance, raw=False, update_fields=None, **kwargs):
    """Store a person's first and last name as clean text, like every other user text.

    Django's User model cannot use the normalizing model field, so this receiver runs
    the same normalizer before each save. Fixture loads (raw) are left alone.
    """
    if raw:
        return
    for name in PERSON_NAME_FIELDS:
        if update_fields is not None and name not in update_fields:
            continue
        value = getattr(instance, name, None)
        if isinstance(value, str):
            max_length = instance._meta.get_field(name).max_length
            setattr(instance, name, clean_for_column(value, max_length))


def count_failed_sign_in(sender, credentials=None, request=None, **kwargs):
    """Every wrong password counts toward the account-wide pause (finding
    bruteforce-per-username-ip), from any address and any sign-in form."""
    from .services import note_failed_sign_in

    username = (credentials or {}).get("username")
    if username:
        note_failed_sign_in(username)


def notice_released_address(sender, request=None, user=None, **kwargs):
    """Tell someone at sign-in that a Google account that proved their address took
    it from this account, which never confirmed it (spec 3.4)."""
    from django.contrib import messages

    from .services import take_released_notice

    if request is not None and user is not None and take_released_notice(user):
        messages.warning(
            request,
            "Your account no longer has an email address: its owner signed in with "
            "Google before you confirmed it. Add an address you own to keep "
            "password reset working.",
        )


def close_chat_sockets_on_sign_out(sender, request=None, user=None, **kwargs):
    """Signing out ends the chat sockets that session opened, at once (WS-01)."""
    from chat.registry import revoke_user_sockets

    session = getattr(request, "session", None)
    session_key = getattr(session, "session_key", None)
    if user is not None and session_key:
        revoke_user_sockets(user.pk, session_key=session_key)


def log_sign_in(sender, request=None, user=None, **kwargs):
    """One security line for each sign-in, whichever way it happened."""
    seclog.event("login_success", request=request, user=user, outcome="success")


def log_failed_sign_in(sender, credentials=None, request=None, **kwargs):
    """One security line for each failed sign-in, with the attempted name (cut to 150
    characters; the logger cleans it). The password is never read."""
    attempted = str((credentials or {}).get("username") or "")[:150]
    seclog.event(
        "login_failed",
        request=request,
        outcome="failure",
        level=logging.WARNING,
        attempted=attempted,
    )


def log_lockout(sender, request=None, username=None, ip_address=None, **kwargs):
    """django-axes locked a name and address pair. The text 'Locking out' stays in the
    line because the lockout alarm matches it."""
    seclog.event(
        "login_locked_out",
        request=request,
        outcome="locked",
        level=logging.WARNING,
        attempted=str(username or "")[:150],
        note="Locking out",
        locked_ip=ip_address,
    )
