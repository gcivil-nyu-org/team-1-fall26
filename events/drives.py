"""Rides and clashes, read from events a page has already fetched (no queries here).

A drive is one parent's ride for a plan they do not attend: the drop-off belongs to the
day that holds the start, the pickup to the day that holds the end (a pickup at exactly
midnight belongs to the day that ends there). The week and day pages show drives in the
driver's own row, and `overlapping` finds the plans that book one person twice (the
week flags in events/flags.py build on both).
"""

from datetime import time, timedelta
from itertools import combinations

DROP_OFF = "Drop-off"
PICKUP = "Pickup"


def legs(event):
    """(name, driver id, time) for each leg of a ride; empty when no ride is needed."""
    if not event.requires_transport:
        return []
    return [
        (DROP_OFF, event.dropoff_driver_id, event.starts_at),
        (PICKUP, event.pickup_driver_id, event.ends_at),
    ]


def leg_day(name, moment, tz):
    """The local date a leg belongs to: a pickup at midnight closes the day before."""
    local = moment.astimezone(tz)
    if name == PICKUP and local.time() == time.min:
        return local.date() - timedelta(days=1)
    return local.date()


def day_drives(events, user_pk, day_start, day_end, own_ids):
    """The rides `user_pk` drives on this day for plans outside `own_ids` (the pks of
    the plans they already have that day), in time order.
    """
    drives = []
    for event in events:
        if event.pk in own_ids:
            continue
        mine = []
        for name, driver_id, moment in legs(event):
            if driver_id != user_pk:
                continue
            on_day = (
                day_start <= moment < day_end
                if name == DROP_OFF
                else day_start < moment <= day_end
            )
            if on_day:
                local = moment.astimezone(day_start.tzinfo)
                mine.append(
                    {
                        "name": name,
                        "at": moment,
                        # A pickup at 00:00 closes the day: "12:00 AM" would read as
                        # early morning, so the pages say "midnight".
                        "midnight": name == PICKUP and local.time() == time.min,
                    }
                )
        if not mine:
            continue
        drives.append(
            {
                "event": event,
                "legs": mine,
                "label": (
                    "both ways"
                    if len(mine) == 2
                    else "drop-off" if mine[0]["name"] == DROP_OFF else "pickup"
                ),
                "times": [leg["at"] for leg in mine],
                "at": mine[0]["at"],
            }
        )
    drives.sort(key=lambda drive: (drive["at"], drive["event"].pk))
    return drives


def overlapping(events):
    """Pairs of timed plans that happen at the same time (plans that only touch do
    not). `events` are one person's plans; all-day plans must be left out.
    """
    ordered = sorted(events, key=lambda event: (event.starts_at, event.pk))
    return [
        (first, second)
        for first, second in combinations(ordered, 2)
        if second.starts_at < first.ends_at
    ]


def overlap_titles(events):
    """{event pk: [titles of the plans it overlaps]} for one person's timed plans."""
    titles = {}
    for first, second in overlapping(events):
        titles.setdefault(first.pk, []).append(second.title)
        titles.setdefault(second.pk, []).append(first.title)
    return titles
