"""Lay out one week as paint chips: a row per family member, a chip per day, deeper colour = fuller day."""

from datetime import datetime, time, timedelta, timezone as datetime_timezone

from .timeline import lane_user_ids

DAYS_IN_WEEK = 7
# Chips show a few plans; the rest is counted ("+2 more") so a busy day keeps its height.
VISIBLE_ITEMS = 2
# An event filling this much of a day (a trip, a school day) paints the chip at full depth.
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


RAIL_FIRST_HOUR = 7
RAIL_LAST_HOUR = 24


def build_today_rail(events, members, today, tz, now):
    """Today's timed plans as top/height percentages of 07:00-24:00, coloured by their first member."""
    day_start, _ = local_day_bounds(today, tz)
    start = (day_start + timedelta(hours=RAIL_FIRST_HOUR)).astimezone(datetime_timezone.utc)
    end = (day_start + timedelta(hours=RAIL_LAST_HOUR)).astimezone(datetime_timezone.utc)
    span = (end - start).total_seconds()

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
        items.append({
            "event": event,
            "people": people,
            "member": people[0] if people else None,
            "top": top,
            "height": round(percent(event.ends_at) - top, 3),
        })
    hours = [
        {"hour": hour % 24, "top": round((hour - RAIL_FIRST_HOUR) / (RAIL_LAST_HOUR - RAIL_FIRST_HOUR) * 100, 3)}
        for hour in range(RAIL_FIRST_HOUR, RAIL_LAST_HOUR, 2)
    ]
    place_side_by_side(items)
    now_top = percent(now) if start <= now < end else None
    return {"items": items, "hours": hours, "now_top": now_top}


# A plan is drawn at least this tall (a 24px tap target on the 612px rail), so short plans
# that touch in time can still collide on screen.
RAIL_MIN_PERCENT = 4


def place_side_by_side(items, min_percent=RAIL_MIN_PERCENT):
    """Give plans that would overlap on screen their own column, sharing the width in that cluster.

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
        column = next((index for index, end in enumerate(ends) if end <= item["top"]), len(ends))
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


def build_week(events, members, week_start, tz, today):
    """Rows of seven day chips, one row per member.

    Events without participants (a family dinner) land on every member's chip, matching how a
    family reads them. `events` should already be limited to the week and prefetch participants.
    """
    days = []
    for offset in range(DAYS_IN_WEEK):
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
                # Subtract in UTC: datetimes sharing a ZoneInfo ignore DST changes when subtracted.
                overlap_start = max(event.starts_at, day["start"]).astimezone(datetime_timezone.utc)
                overlap_end = min(event.ends_at, day["end"]).astimezone(datetime_timezone.utc)
                items.append({
                    "event": event,
                    "all_day": event.starts_at <= day["start"] and event.ends_at >= day["end"],
                    "starts_earlier": event.starts_at < day["start"],
                    "is_long": overlap_end - overlap_start >= LONG_EVENT,
                    "tentative": event.is_tentative,
                })
            chips.append({
                **day,
                "level": chip_level(items),
                "items": items[:VISIBLE_ITEMS],
                "more": max(len(items) - VISIBLE_ITEMS, 0),
                "count": len(items),
                # A day whose only plans are unconfirmed gets a dashed edge.
                "tentative": bool(items) and all(item["tentative"] for item in items),
            })
        rows.append({**member, "chips": chips})
    return {"days": days, "rows": rows}
