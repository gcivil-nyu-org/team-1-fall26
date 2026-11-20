"""One safe way to read a moment on a local wall clock."""

from datetime import timezone as datetime_timezone

from django.utils import timezone


def localtime_or_utc(moment, tz=None):
    """timezone.localtime(moment, tz), or the UTC time when the local time falls
    outside the years Python can hold (a plan near year 9999 in a zone ahead of UTC)."""
    try:
        return timezone.localtime(moment, tz)
    except (OverflowError, ValueError):
        return moment.astimezone(datetime_timezone.utc)
