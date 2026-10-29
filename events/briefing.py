"""The "Your day" briefing: the plans left today (or tomorrow's, once today is done) and who drives.

Kids see their own plans and whole-family ones. Parents see the whole family's, with their own
drives and the rides nobody has taken yet on top, and the plans that double-book someone flagged.
"""

from datetime import timedelta

from django.db.models import Q

from .access import family_events
from .models import Event
from .week import local_day_bounds

# Two days of one family's plans; more than this is a data problem, not a schedule.
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


def _briefing(events, now, tz):
    """Today's plans that haven't ended; once there are none, tomorrow's, with a note on why."""
    today = now.astimezone(tz).date()
    today_start, today_end = local_day_bounds(today, tz)
    tomorrow = today + timedelta(days=1)
    _, tomorrow_end = local_day_bounds(tomorrow, tz)
    events = list(events.filter(starts_at__lt=tomorrow_end, ends_at__gt=today_start).order_by("starts_at", "pk")[:MAX_PLANS])
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


def build_kid_briefing(user, workspace, now, tz):
    """A kid's own plans and whole-family ones."""
    events = (
        family_events(user)
        .filter(workspace=workspace)
        .filter(Q(participants=user) | Q(participants__isnull=True))
        .distinct()
        .select_related("dropoff_driver", "pickup_driver")
    )
    return _briefing(events, now, tz)


def build_family_briefing(user, workspace, now, tz):
    """A parent's view: every family plan, their drives, the rides with no driver, and clashes."""
    events = (
        family_events(user)
        .filter(workspace=workspace)
        .select_related("dropoff_driver", "pickup_driver")
        .prefetch_related("participants")
    )
    briefing = _briefing(events, now, tz)
    my_drives, open_rides = [], 0
    for item in briefing["items"]:
        event = item["event"]
        item["people"] = list(event.participants.all())
        if not event.requires_transport:
            continue
        for leg, driver, at in (("Drop-off", event.dropoff_driver, event.starts_at), ("Pickup", event.pickup_driver, event.ends_at)):
            if at < now:
                continue  # this morning's drop-off is done; only rides still ahead count
            if driver is None:
                open_rides += 1
            elif driver.pk == user.pk:
                my_drives.append({"leg": leg, "event": event, "at": at})
    my_drives.sort(key=lambda drive: drive["at"])
    _flag_clashes(briefing["items"])
    briefing.update(
        for_parent=True,
        transport_modes=Event.TransportMode.choices,
        my_drives=my_drives,
        open_rides=open_rides,
        clashes=sum(1 for item in briefing["items"] if item["clashes"]),
    )
    return briefing


def _flag_clashes(items):
    """Mark plans that book the same person twice at once (touching edges and all-day plans don't count)."""
    for item in items:
        item["clashes"] = []
    timed = [item for item in items if not item["all_day"]]
    for index, first in enumerate(timed):
        for second in timed[index + 1:]:
            if second["event"].starts_at >= first["event"].ends_at:
                continue
            shared = {person.pk for person in first["people"]} & {person.pk for person in second["people"]}
            if not shared:
                continue
            people = [person for person in first["people"] if person.pk in shared]
            first["clashes"].append({"event": second["event"], "people": people})
            second["clashes"].append({"event": first["event"], "people": people})
