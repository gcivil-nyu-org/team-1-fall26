"""Small display helpers for the paint-chip pages: people by first name, and calm day
labels.
"""

import hashlib
from datetime import timedelta
from functools import lru_cache

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.html import format_html

register = template.Library()


@register.filter
def person(user):
    """The name a family uses: the first name when there is one, else the username."""
    if not user:
        return ""
    return (getattr(user, "first_name", "") or "").strip() or user.get_username()


@register.filter
def initial(user):
    """One letter for the round member dot."""
    return person(user)[:1].upper()


@register.filter
def day_label(moment, fmt="D j M"):
    """ "Today", "Yesterday", "Tomorrow", or the date ("Mon 26 Oct" by default), in the
    current time zone.
    """
    if not moment:
        return ""
    day = timezone.localtime(moment).date() if hasattr(moment, "hour") else moment
    today = timezone.localdate()
    names = {
        today: "Today",
        today - timedelta(days=1): "Yesterday",
        today + timedelta(days=1): "Tomorrow",
    }
    return names.get(day) or date_format(day, fmt)


@register.filter(expects_localtime=True)
def clock(moment):
    """A <time> that reads "4:00 PM"; the top bar's 12h/24h switch can rewrite it."""
    if not moment:
        return ""
    return format_html(
        '<time datetime="{}" data-clock="{}">{}</time>',
        moment.isoformat(),
        date_format(moment, "H:i"),
        date_format(moment, "g:i A"),
    )


@register.filter
def member(value, members):
    """The family member (hue, picked colour) for a user id, such as a checkbox value,
    from {pk: member}.
    """
    key = getattr(value, "value", value)
    try:
        return (members or {}).get(int(str(key)))
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=None)
def _content_hash(path):
    found = finders.find(path)
    if not found:
        return "0"
    with open(found, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:10]


@register.simple_tag
def asset(path):
    """A static file's URL stamped with a hash of its content, so browsers fetch a new
    copy whenever the file changes (nginx sends no cache headers, only an ETag).
    """
    digest = _content_hash.__wrapped__(path) if settings.DEBUG else _content_hash(path)
    return f"{static(path)}?v={digest}"
