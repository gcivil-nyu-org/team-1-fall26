"""Log safety and context (hardening spec 5 and 10.2).

Three jobs live here:

* `clean_log_text` turns control characters into visible escapes and hides tokens in
  paths and query strings, so no value can forge a log line or leak a credential.
* `SafeLogRecord` runs that cleaning on the final message of every record in the
  process, whichever logger or handler sees it. Libraries such as django-axes and
  Django's own request logger therefore need no special case.
* The request id and the request itself live in context variables, so any log line
  written while a request runs can say which request it belongs to.
"""

import logging
import re
import uuid
from contextvars import ContextVar

REDACTED = "<redacted>"
MAX_LOG_FIELD = 300

request_id_var = ContextVar("syncmily_request_id", default="-")
request_var = ContextVar("syncmily_request", default=None)

_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

# Tokens that travel in URL paths: invite links and password-reset links.
_PATH_SECRETS = (
    re.compile(r"(/accounts/invite/)[^/\s?\"'<>]+"),
    re.compile(r"(/accounts/reset/)[^/\s?\"'<>]+(?:/[^/\s?\"'<>]+)?"),
)
# A query string: "?key=value" with at least one key. It may hold tokens or codes.
_QUERY = re.compile(r"(?<=[\w/])\?[\w.%\-]+=[^\s\"'<>]*")
# Anything that looks like a credential in "name=value" form.
_SECRET_PAIR = re.compile(
    r"(?i)\b(password\d?|passwd|token|secret|sessionid|csrftoken|"
    r"csrfmiddlewaretoken|api[_-]?key|code)=[^\s&\"'<>]+"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_ESCAPES = {"\r": "\\r", "\n": "\\n", "\t": "\\t", "\x1b": "\\x1b"}


def _escape(match):
    char = match.group(0)
    if char in _ESCAPES:
        return _ESCAPES[char]
    code = ord(char)
    return f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}"


def clean_log_text(value):
    """Return `value` as one safe line: tokens hidden, control characters escaped."""
    text = value if isinstance(value, str) else str(value)
    for pattern in _PATH_SECRETS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    text = _QUERY.sub("?" + REDACTED, text)
    text = _SECRET_PAIR.sub(lambda m: m.group(1) + "=" + REDACTED, text)
    return _CONTROL.sub(_escape, text)


def clean_field(value, limit=MAX_LOG_FIELD):
    """Clean one value for a key=value field: cut it, quote it if it has spaces."""
    text = clean_log_text(value)[:limit]
    if not text:
        return '""'
    if re.search(r'[\s="]', text):
        return '"' + text.replace('"', "'") + '"'
    return text


class SafeLogRecord(logging.LogRecord):
    """A record whose final message is always safe to print."""

    def getMessage(self):
        try:
            message = super().getMessage()
        except Exception:  # a bad format string must not break the caller
            message = str(self.msg)
        return clean_log_text(message)


_installed = False


def install():
    """Make every new record a SafeLogRecord. Safe to call more than once."""
    global _installed
    if _installed:
        return
    _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.__class__ = SafeLogRecord
        return record

    logging.setLogRecordFactory(factory)


# --- request context ---------------------------------------------------------------


def new_request_id(incoming=None):
    """Keep a well-formed id from the proxy, else make one."""
    if incoming and _REQUEST_ID_OK.match(incoming):
        return incoming
    return uuid.uuid4().hex


def current_user_id(request):
    """The signed-in user's id, without ever triggering a database read."""
    user = getattr(request, "_cached_user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.pk
    return None


def client_address(request):
    from .ratelimit import client_ip

    try:
        return client_ip(request)
    except Exception:
        return "-"


class ContextFilter(logging.Filter):
    """Adds request_id, user_id, client_ip, route, event and outcome to each record."""

    def filter(self, record):
        request = request_var.get()
        record.request_id = request_id_var.get()
        if not hasattr(record, "event"):
            record.event = "-"
        if not hasattr(record, "outcome"):
            record.outcome = "-"
        if request is not None:
            user_id = current_user_id(request)
            record.user_id = user_id if user_id is not None else "-"
            record.client_ip = client_address(request)
            record.route = clean_field(getattr(request, "path", "-"), 200)
        else:
            record.user_id = "-"
            record.client_ip = "-"
            record.route = "-"
        return True


class SafeFormatter(logging.Formatter):
    """Formats as key=value text and cleans tracebacks the same way as messages."""

    def formatException(self, exc_info):
        # One line, so an exception message cannot start a forged record.
        return clean_log_text(super().formatException(exc_info))
