"""Shared helpers for the gap4 lane (security event logging, error handling, CSRF sweep).

This is NOT a test module. test_logging_csrf.py (broad sweep) and test_gap4.py (the
confirmed findings) both import from here, so no TestCase is collected twice.

How production logging is modelled
    Procfile starts `daphne ... syncmily.asgi:application` with no -v flag and settings.py
    has no LOGGING. daphne's CLI then runs logging.basicConfig(level=INFO, format=log_fmt)
    on the ROOT logger (daphne/cli.py:234), and writes an NCSA access log to stdout (client
    address, method, path, status; no user). capture() reproduces exactly that: root level
    INFO plus one handler that formats with daphne's default log_fmt. Django's own
    'django.*' loggers propagate to that root handler. DEBUG is False under the test
    runner, as in production.
"""

import hashlib
import logging
import re
from contextlib import contextmanager
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import serializers
from django.utils import timezone

from accounts.models import Membership, Workspace
from checklists.models import Checklist, ChecklistItem
from events.models import Event

PW = "Gap4-Log-Csrf-2026!"
IP = "198.51.100.23"  # REMOTE_ADDR used for "the attacker / the visitor"

try:  # daphne's own default format, read from its parser rather than copied
    from daphne.cli import CommandLineInterface

    DAPHNE_FMT = CommandLineInterface().parser.get_default("log_fmt")
except Exception:  # pragma: no cover
    DAPHNE_FMT = "%(asctime)-15s %(levelname)-8s %(message)s"

NAMED_LOGGERS = (
    "django",
    "django.request",
    "django.security",
    "django.security.csrf",
    "django.security.DisallowedHost",
    "axes",
    "accounts",
    "events",
    "chat",
    "checklists",
    "calendars",
    "syncmily",
)

# Secrets registered by the tests; describe() masks them before printing a record.
SECRETS = set()


def register_secret(value):
    if value and len(str(value)) >= 6:
        SECRETS.add(str(value))


def redact(text):
    for secret in sorted(SECRETS, key=len, reverse=True):
        text = text.replace(secret, "<SECRET>")
    return text


class Capture(logging.Handler):
    """Collects every record that reaches the root logger."""

    def __init__(self):
        super().__init__(level=0)
        self.records = []
        self.formatter = logging.Formatter(DAPHNE_FMT)

    def emit(self, record):
        try:
            record.gap4_text = self.format(record)
        except Exception:  # pragma: no cover
            record.gap4_text = "<unformattable record>"
        self.records.append(record)

    # views over the capture -------------------------------------------------
    def lines(self):
        """Formatted lines, exactly as the production root handler would print them."""
        return [r.gap4_text for r in self.records]

    def messages(self):
        return [r.getMessage() for r in self.records]


@contextmanager
def capture():
    """Attach a capturing handler the way daphne's basicConfig would see the app."""
    root = logging.getLogger()
    handler = Capture()
    old_level = root.level
    root.setLevel(logging.INFO)  # daphne/cli.py: basicConfig(level=INFO) at verbosity 1
    root.addHandler(handler)
    extra = []
    for name in NAMED_LOGGERS:
        logger = logging.getLogger(name)
        if not logger.propagate:  # would hide records from root: listen there too
            logger.addHandler(handler)
            extra.append(logger)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        for logger in extra:
            logger.removeHandler(handler)


def describe(records):
    """Human-readable, redacted dump of captured records for assertion messages."""
    if not records:
        return "  (no log record was emitted at all)"
    out = []
    for r in records:
        text = redact(r.getMessage()).replace("\n", "\\n").replace("\r", "\\r")
        out.append(f"  [{r.levelname}] {r.name}: {text[:300]}")
    return "\n".join(out)


OUTCOME = re.compile(
    r"fail|success|succeed|denied|forbidden|refus|lock|block|too many|rate|reject|"
    r"invalid|removed|accepted|used|not found|disallowed|missing|incorrect|expired|"
    r"granted|created|changed|reset|sent|issued|wrong|unauthori|closed|error|4403|404|403|429",
    re.I,
)


def _mentions(message, token):
    """A bare number (a user pk) only counts when labelled user/uid/id/pk, so the digit in
    a UUID or an event id in the path cannot pass for 'who'."""
    token = str(token)
    if token.isdigit():
        return (
            re.search(rf"(user|uid|id|pk)\W{{0,3}}{token}\b", message, re.I) is not None
        )
    return token.lower() in message.lower()


def judge(handler, *, what, who, where=()):
    """
    Check the captured records for ONE record that carries all of
        what     regex describing the event type
        who      any of these strings (username, user id, client IP, attempted name)
        where    any of these strings (route path or client IP); default = `who` IP/path
        outcome  a success/failure/denied word
        when     always present on the record (record.created) and printed by daphne's
                 log_fmt (%(asctime)s), checked once in the sink test
    Returns (ok, report). report names the best record's missing elements.
    """
    best = None
    for record in handler.records:
        message = record.getMessage()
        missing = []
        if not re.search(what, message, re.I):
            missing.append("what")
        if not any(_mentions(message, w) for w in who if w):
            missing.append("who")
        if where and not any(str(w).lower() in message.lower() for w in where if w):
            missing.append("where")
        if not OUTCOME.search(message):
            missing.append("outcome")
        if not missing:
            return True, ""
        if best is None or len(missing) < len(best[1]):
            best = (record, missing)
    if best is None:
        return False, "no log record was emitted for this event"
    record, missing = best
    return False, (
        f"closest record lacks {missing}: [{record.levelname}] {record.name}: "
        f"{redact(record.getMessage())[:200]}"
    )


# ---------------------------------------------------------------------------
# World: two households, every role the sweeps need
# ---------------------------------------------------------------------------

User = get_user_model()


def build_world(cls):
    now = timezone.now()

    def user(name, **extra):
        return User.objects.create_user(
            username=name, password=PW, email=f"{name.lower()}@gap4.example", **extra
        )

    def member(workspace, who, role, admin, accepted=True):
        return Membership.objects.create(
            workspace=workspace,
            user=who,
            role=role,
            invited_by=admin,
            accepted_at=now if accepted else None,
        )

    cls.admin = user("LogAdminA")
    cls.parent = user("LogParentA")
    cls.kid = user("LogKidA")
    cls.admin_b = user("LogAdminB")
    cls.parent_b = user("LogParentB")
    cls.loner = user("LogLoner")  # signed up, no household
    cls.ws = Workspace.objects.create(name="Gap4 A family", primary_admin=cls.admin)
    cls.ws_b = Workspace.objects.create(name="Gap4 B family", primary_admin=cls.admin_b)
    member(cls.ws, cls.parent, Membership.Role.PARENT, cls.admin)
    member(cls.ws, cls.kid, Membership.Role.KID, cls.admin)
    member(cls.ws_b, cls.parent_b, Membership.Role.PARENT, cls.admin_b)

    pending_user = User.objects.create_user(
        username="LogPendingKid", password=None, is_active=False
    )
    cls.pending = member(
        cls.ws, pending_user, Membership.Role.KID, cls.admin, accepted=False
    )

    start = (now + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)

    def event(workspace, creator, title):
        ev = Event.objects.create(
            created_by=creator,
            workspace=workspace,
            title=title,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            location="Prospect Park",
            requires_transport=True,
            dropoff_driver=creator,
            pickup_driver=creator,
            latitude=40.6602,
            longitude=-73.9690,
        )
        ev.participants.set([creator])
        return ev

    cls.event = event(cls.ws, cls.admin, "Gap4 A event")
    cls.event_b = event(cls.ws_b, cls.admin_b, "Gap4 B event")
    cls.checklist = Checklist.objects.create(
        workspace=cls.ws, title="Gap4 list", created_by=cls.admin
    )
    cls.item = ChecklistItem.objects.create(
        checklist=cls.checklist, description="Gap4 item", position=0
    )


# ---------------------------------------------------------------------------
# State snapshot (the idea from test_authz.py: hash every table a request may touch)
# ---------------------------------------------------------------------------


def snapshot():
    from django.apps import apps

    digest = hashlib.sha256()
    for label in (
        "auth.user",
        "accounts.workspace",
        "accounts.membership",
        "accounts.membercolor",
        "accounts.googleidentity",
        "accounts.briefingseen",
        "events.event",
        "events.tripstart",
        "chat.chatmessage",
        "checklists.checklist",
        "checklists.checklistitem",
        "calendars.calendarconnection",
        "otp_totp.totpdevice",
        "otp_static.staticdevice",
        "otp_static.statictoken",
    ):
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        kwargs = {}
        if label == "auth.user":
            # last_login moves on every sign-in; it is not tenant data.
            kwargs["fields"] = [
                "username",
                "password",
                "email",
                "is_active",
                "is_staff",
                "is_superuser",
                "first_name",
                "last_name",
            ]
        digest.update(
            label.encode()
            + serializers.serialize(
                "json", model.objects.order_by("pk"), **kwargs
            ).encode()
        )
    return digest.hexdigest()
