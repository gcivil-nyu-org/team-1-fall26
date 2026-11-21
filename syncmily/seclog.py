"""One place to write security-event log lines (hardening spec 10.2).

    seclog.event("login_failed", request=request, outcome="failure", attempted="x")

A line carries the event name, the outcome, who (user id and name, or the attempted
name), where from (client address) and which route. Extra keyword arguments are ids
and reason codes only. Never pass a password, code, token, session id, CSRF token,
raw user text or coordinates. Every value is cleaned, cut short and quoted when it
holds spaces, so a value cannot add fields or lines.
"""

import logging

from .logs import clean_field, client_address, current_user_id, request_var

logger = logging.getLogger("syncmily.security")

MAX_NAME = 150


def _user_fields(user):
    if user is None or not getattr(user, "pk", None):
        return {}
    return {"user_id": user.pk, "user": getattr(user, "username", "")[:MAX_NAME]}


def event(name, *, request=None, user=None, outcome, level=logging.INFO, **ids):
    """Write one security-event line. `request` defaults to the request being served,
    and `user` to its signed-in user."""
    if request is None:
        request = request_var.get()
    if user is None and request is not None:
        cached = getattr(request, "_cached_user", None)
        if current_user_id(request) is not None:
            user = cached
    fields = {"event": name, "outcome": outcome}
    fields.update(_user_fields(user))
    if request is not None:
        fields["client_ip"] = client_address(request)
        fields["route"] = getattr(request, "path", "-")
    fields.update({key: value for key, value in ids.items() if value is not None})
    text = " ".join(f"{key}={clean_field(value)}" for key, value in fields.items())
    logger.log(
        level,
        "security %s",
        text,
        extra={"event": clean_field(name), "outcome": clean_field(outcome)},
    )
