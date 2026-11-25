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

from syncmily.limits import RENDER
from syncmily.text import truncate_graphemes

register = template.Library()


@register.filter
def possessive(name):
    """'The Garcias' -> 'The Garcias’' and 'Ana' -> 'Ana’s', for week headings."""
    name = str(name).strip()
    return f"{name}’" if name.endswith(("s", "S")) else f"{name}’s"


def full_name(user):
    """The name a family uses, uncut: the first name when there is one, else the
    username."""
    if not user:
        return ""
    return (getattr(user, "first_name", "") or "").strip() or user.get_username()


@register.filter
def person(user):
    """The name a family uses, cut to 30 characters with an ellipsis. Names sit in
    pills, chips and dots, where 150 characters have no room."""
    return truncate_graphemes(full_name(user), RENDER.display_name_chars)


@register.filter
def initial(user):
    """One character for the round member dot: the first letter or digit of the name,
    or "?" when it has none."""
    for character in full_name(user):
        if character.isalnum():
            return character.upper()
    return "?"


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
    copy whenever the file changes. nginx lets browsers keep stamped files for a year
    (.platform/hooks/postdeploy/03_https.sh).
    """
    digest = _content_hash.__wrapped__(path) if settings.DEBUG else _content_hash(path)
    return f"{static(path)}?v={digest}"


@register.filter
def leave_marker(event):
    """True when an event gets a live "Leave by" marker on timelines: subway or bus,
    a map pin, and still to come."""
    from events.models import Event

    return (
        event.transport_mode == Event.TransportMode.TRANSIT
        and event.has_valid_coordinates
        and event.starts_at > timezone.now()
    )


@register.filter
def ahead(moment):
    """True while a moment is still to come: the week's flags count the same legs."""
    return moment >= timezone.now()


@register.filter
def has_person(people, user):
    """True when `user` is among the member dicts in `people`."""
    return any(getattr(person.get("user"), "pk", None) == user.pk for person in people)
