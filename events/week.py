"""Lay out one week as paint chips: a row per family member, a chip per day, deeper
colour = fuller day.
"""

from datetime import datetime, time, timedelta, timezone as datetime_timezone

from .timeline import lane_user_ids

DAYS_IN_WEEK = 7
# Chips show a few plans; the rest is counted ("+2 more") so a busy day keeps its
# height.
VISIBLE_ITEMS = 2
# An event filling this much of a day (a trip, a school day) paints the chip at full
# depth.
LONG_EVENT = timedelta(hours=6)
FULL_LEVEL = 4


def week_start_for(day):
    """The Monday on or before the given date."""
    return day - timedelta(days=day.weekday())


def local_day_bounds(day, tz):
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def chip_level(items):
    """0 = free day, 1-4 = how full it is. A long event alone fills the day."""
    if any(item["is_long"] for item in items):
        return FULL_LEVEL
    return min(len(items), FULL_LEVEL)


# The rail shows only the hours around today's plans: an hour before the first one and
# an hour after the last, at least RAIL_MIN_HOURS tall. An empty day draws no rail at
# all (it listed all 24 empty hours before). Each hour is RAIL_HOUR_PX tall.
RAIL_MIN_HOURS = 4
RAIL_HOUR_PX = 26
RAIL_MIN_PX = 24  # the shortest a plan is drawn: a 24px tap target


def rail_window(events, day_start, day_end):
    """The (first, last) local hours the rail covers; None when nothing is planned."""
    starts, ends = [], []
    for event in events:
        if event.starts_at < day_end and event.ends_at > day_start:
            starts.append((max(event.starts_at, day_start) - day_start).total_seconds())
            ends.append((min(event.ends_at, day_end) - day_start).total_seconds())
    if not starts:
        return None
    first = max(0, int(min(starts) // 3600) - 1)
    last = min(24, -int(-max(ends) // 3600) + 1)
    if last - first < RAIL_MIN_HOURS:
        last = min(24, first + RAIL_MIN_HOURS)
        first = max(0, last - RAIL_MIN_HOURS)
    return first, last


def build_today_rail(events, members, today, tz, now):
    """Today's timed plans as top/height percentages of the rail's window (see
    rail_window), coloured by their first member.
    """
    day_start, day_end = local_day_bounds(today, tz)
    window = rail_window(events, day_start, day_end)
    if window is None:
        return {"items": [], "hours": [], "now_top": None, "height": 0}
    first, last = window
    start = (day_start + timedelta(hours=first)).astimezone(datetime_timezone.utc)
    end = (day_start + timedelta(hours=last)).astimezone(datetime_timezone.utc)
    span = (end - start).total_seconds()
    height = (last - first) * RAIL_HOUR_PX

    def percent(moment):
        seconds = (min(max(moment, start), end) - start).total_seconds()
        return round(seconds / span * 100, 3)

    by_id = {member["user"].pk: member for member in members}
    items = []
    for event in events:
        if not (event.starts_at < end and event.ends_at > start):
            continue
        people = [by_id[pk] for pk in lane_user_ids(event, by_id)]
        top = percent(event.starts_at)
        items.append(
            {
                "event": event,
                "people": people,
                "member": people[0] if people else None,
                "top": top,
                "height": round(percent(event.ends_at) - top, 3),
            }
        )
    hours = [
        {"hour": hour % 24, "top": round((hour - first) / (last - first) * 100, 3)}
        for hour in range(first, last)
    ]
    place_side_by_side(items, min_percent=RAIL_MIN_PX / height * 100)
    now_top = percent(now) if start <= now < end else None
    return {"items": items, "hours": hours, "now_top": now_top, "height": height}


# A plan is drawn at least RAIL_MIN_PX tall, so short plans that touch in time can still
# collide on screen; build_today_rail passes that height as a share of its rail.
RAIL_MIN_PERCENT = 4


def place_side_by_side(items, min_percent=RAIL_MIN_PERCENT):
    """Give plans that would overlap on screen their own column, sharing the width in
    that cluster.

    `min_percent` is the smallest height a plan is drawn at, as a share of the axis.
    """
    items.sort(key=lambda item: (item["top"], -item["height"]))
    clusters = []
    for item in items:
        bottom = item["top"] + max(item["height"], min_percent)
        if not clusters or item["top"] >= clusters[-1]["end"]:
            clusters.append({"items": [], "column_ends": [], "end": bottom})
        cluster = clusters[-1]
        ends = cluster["column_ends"]
        column = next(
            (index for index, end in enumerate(ends) if end <= item["top"]), len(ends)
        )
        if column == len(ends):
            ends.append(bottom)
        else:
            ends[column] = bottom
        item["column"] = column
        cluster["items"].append(item)
        cluster["end"] = max(cluster["end"], bottom)
    for cluster in clusters:
        for item in cluster["items"]:
            item["columns"] = len(cluster["column_ends"])


def build_week(events, members, week_start, tz, today, show_weekends=True):
    """Rows of seven day chips, one row per member.

    Events without participants (a family dinner) land on every member's chip, matching
    how a
    family reads them. `events` should already be limited to the week and prefetch
    participants.
    """
    days = []
    for offset in range(DAYS_IN_WEEK if show_weekends else 5):
        day = week_start + timedelta(days=offset)
        start, end = local_day_bounds(day, tz)
        days.append({"date": day, "start": start, "end": end, "is_today": day == today})

    member_ids = {member["user"].pk for member in members}
    placed = [(event, lane_user_ids(event, member_ids)) for event in events]

    rows = []
    for member in members:
        chips = []
        for day in days:
            items = []
            for event, ids in placed:
                if ids and member["user"].pk not in ids:
                    continue
                if not (event.starts_at < day["end"] and event.ends_at > day["start"]):
                    continue
                # Subtract in UTC: datetimes sharing a ZoneInfo ignore DST changes when
                # subtracted.
                overlap_start = max(event.starts_at, day["start"]).astimezone(
                    datetime_timezone.utc
                )
                overlap_end = min(event.ends_at, day["end"]).astimezone(
                    datetime_timezone.utc
                )
                items.append(
                    {
                        "event": event,
                        "all_day": (
                            event.starts_at <= day["start"]
                            and event.ends_at >= day["end"]
                        ),
                        "starts_earlier": event.starts_at < day["start"],
                        "is_long": overlap_end - overlap_start >= LONG_EVENT,
                        "tentative": event.is_tentative,
                    }
                )
            chips.append(
                {
                    **day,
                    "level": chip_level(items),
                    "items": items[:VISIBLE_ITEMS],
                    "more": max(len(items) - VISIBLE_ITEMS, 0),
                    "count": len(items),
                    # A day whose only plans are unconfirmed gets a dashed edge.
                    "tentative": (
                        bool(items) and all(item["tentative"] for item in items)
                    ),
                }
            )
        rows.append({**member, "chips": chips})
    return {"days": days, "rows": rows}
