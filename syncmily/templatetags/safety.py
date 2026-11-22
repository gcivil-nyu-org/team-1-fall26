"""Template filters that keep user text in its box: {% load safety %}.

    {{ event.title|bdi }}         <bdi>Title</bdi>, so right-to-left text cannot
                                  reorder the labels around it
    {{ user.first_name|short_name }}  at most 30 characters inside <bdi>, with the
                                  full name in a title attribute when it was cut
    {{ event.title|clamp:80 }}    at most 80 characters, cut between whole letters

format_html escapes every value, so the filters never let markup through.
"""

from django import template
from django.utils.html import format_html

from syncmily import limits
from syncmily.text import truncate_graphemes

register = template.Library()


def _text(value):
    return "" if value is None else str(value)


@register.filter
def bdi(value):
    """Wrap text in <bdi> so its direction stays inside it."""
    return format_html("<bdi>{}</bdi>", _text(value))


@register.filter
def short_name(value, length=limits.RENDER.display_name_chars):
    """A name cut to `length` characters (30 by default) inside <bdi>. When it was
    cut, the full name sits in the title attribute."""
    full = _text(value)
    short = truncate_graphemes(full, int(length))
    if short == full:
        return format_html("<bdi>{}</bdi>", full)
    return format_html('<bdi title="{}">{}</bdi>', full, short)


@register.filter
def clamp(value, length):
    """Cut text to `length` characters without splitting a letter from its accents
    or an emoji sequence. The result is plain text, escaped as usual."""
    return truncate_graphemes(_text(value), int(length))
