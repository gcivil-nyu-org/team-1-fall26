"""A kid's "Your day" briefing: the plans left today (or tomorrow's, once today is done) and who drives."""

from datetime import timedelta

from django.db.models import Q

from .access import family_events
from .week import local_day_bounds

# Two days of one kid's plans; more than this is a data problem, not a schedule.
MAX_PLANS = 40


def _items(events, day_start, day_end):
    return [
        {
            "event": event,
            "all_day": event.starts_at <= day_start and event.ends_at >= day_end,
            "starts_earlier": event.starts_at < day_start,
        }
        for event in events
    ]


def build_kid_briefing(user, workspace, now, tz):
    """Today's plans that haven't ended; once there are none, tomorrow's, with a note on why."""
    today = now.astimezone(tz).date()
    today_start, today_end = local_day_bounds(today, tz)
    tomorrow = today + timedelta(days=1)
    _, tomorrow_end = local_day_bounds(tomorrow, tz)
    events = list(
        family_events(user)
        .filter(workspace=workspace, starts_at__lt=tomorrow_end, ends_at__gt=today_start)
        .filter(Q(participants=user) | Q(participants__isnull=True))
        .distinct()
        .select_related("dropoff_driver", "pickup_driver")
        .order_by("starts_at", "pk")[:MAX_PLANS]
    )
    today_events = [event for event in events if event.starts_at < today_end]
    left_today = [event for event in today_events if event.ends_at > now]
    if left_today:
        return {"day": today, "is_today": True, "items": _items(left_today, today_start, today_end)}
    return {
        "day": tomorrow,
        "is_today": False,
        "done_today": bool(today_events),
        "items": _items([event for event in events if event.ends_at > today_end], today_end, tomorrow_end),
    }
