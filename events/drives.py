"""Rides and clashes, read from events a page has already fetched (no queries here).

A drive is one parent's ride for a plan they do not attend: the drop-off belongs to the
day that holds the start, the pickup to the day that holds the end (a pickup at exactly
midnight belongs to the day that ends there). The week and day pages show drives in the
driver's own row, and `overlapping` finds the plans that book one person twice (the
week flags in events/flags.py build on both).
"""

from bisect import bisect_left, bisect_right
from datetime import time, timedelta

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
    """One person's timed plans in time order, each as (plan, earlier, later): how many
    plans that began before it are still on when it starts, and how many more start
    before it ends (plans that only touch do not overlap). All-day plans must be left
    out.

    Sorted once and counted with bisect, so a week of 2,000 plans costs n log n, not
    the two million pairs it holds.
    """
    ordered = sorted(events, key=lambda event: (event.starts_at, event.pk))
    starts = [event.starts_at for event in ordered]
    ends = sorted(event.ends_at for event in ordered)
    return [
        (
            event,
            # Every plan that ended by this start began before it, so the rest of the
            # plans ahead of it in the order are still on.
            index - bisect_right(ends, event.starts_at),
            bisect_left(starts, event.ends_at) - index - 1,
        )
        for index, event in enumerate(ordered)
    ]


def overlap_titles(events, limit):
    """{event pk: [titles of the plans it overlaps]} for one person's timed plans: the
    first `limit` in time order, then "N more".
    """
    counted = overlapping(events)
    titles = {}
    still_on = []  # earlier plans, in time order, dropped once they are seen to be over
    for index, (event, earlier, later) in enumerate(counted):
        names = []
        position = 0
        while position < len(still_on) and len(names) < limit:
            if still_on[position].ends_at > event.starts_at:
                names.append(still_on[position].title)
                position += 1
            else:
                # Over for good: every plan still to come starts later.
                del still_on[position]
        still_on.append(event)
        following = counted[index + 1 : index + 1 + min(later, limit - len(names))]
        names += [other.title for other, _, _ in following]
        rest = earlier + later - len(names)
        if earlier or later:
            titles[event.pk] = names + ([f"{rest} more"] if rest else [])
    return titles
