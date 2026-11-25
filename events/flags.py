"""The week's flags for parents: rides still ahead with no driver, and double-bookings.

Computed from the events the dashboard has already fetched, so they cost no query.
"""

from datetime import timedelta

from .drives import legs, leg_day, overlapping
from .timeline import lane_user_ids
from .week import local_day_bounds


def _all_day(event, day_start, day_end):
    return event.starts_at <= day_start and event.ends_at >= day_end


def build_week_flags(events, members, week_start, tz, now):
    """What needs attention in the week on screen (Monday to Sunday, even when the
    weekend columns are hidden): rides still ahead with no driver, and the people with
    two timed plans at once whose later plan has not ended. None when there is nothing
    to say.
    """
    days = [week_start + timedelta(days=offset) for offset in range(7)]
    bounds = {day: local_day_bounds(day, tz) for day in days}
    week_open, week_close = bounds[days[0]][0], bounds[days[-1]][1]
    member_ids = {member["user"].pk for member in members}

    open_rides, open_days = 0, set()
    for event in events:
        for name, driver_id, moment in legs(event):
            if driver_id is not None or moment < now:
                continue
            day = leg_day(name, moment, tz)
            if day in bounds:
                open_rides += 1
                open_days.add(day)

    in_week = [
        event
        for event in events
        if event.starts_at < week_close and event.ends_at > week_open
    ]
    double_booked = []
    for member in members:
        pk = member["user"].pk
        mine = [event for event in in_week if pk in lane_user_ids(event, member_ids)]
        clash_days = []
        for day in days:
            day_start, day_end = bounds[day]
            # Two plans that both touch a day and overlap do so on that day.
            timed = [
                event
                for event in mine
                if event.starts_at < day_end
                and event.ends_at > day_start
                and not _all_day(event, day_start, day_end)
            ]
            if any(
                earlier and event.ends_at > now
                for event, earlier, _ in overlapping(timed)
            ):
                clash_days.append(day)
        if clash_days:
            double_booked.append({"member": member, "days": clash_days})

    if not open_rides and not double_booked:
        return None
    return {
        "open_rides": open_rides,
        "open_days": sorted(open_days),
        "double_booked": double_booked,
    }
