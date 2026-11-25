"""The month grid: whole Monday-to-Sunday weeks around one month."""

from datetime import timedelta

from .timeline import lane_user_ids
from .week import local_day_bounds, week_start_for

VISIBLE_PER_DAY = 3
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def build_month(events, members, month_start, tz, today, show_weekends=True):
    """Weeks of day cells for the month that starts on `month_start`.

    Each plan carries the colour of its first family member who is going; plans for the
    whole family carry none. `events` should cover the whole grid, not just the month,
    and prefetch participants.
    """
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    grid_start = week_start_for(month_start)
    by_id = {member["user"].pk: member for member in members}
    placed = [(event, lane_user_ids(event, set(by_id))) for event in events]

    weeks = []
    week_start = grid_start
    while week_start < next_month:
        days = []
        for offset in range(7):
            if offset >= 5 and not show_weekends:
                continue
            day = week_start + timedelta(days=offset)
            start, end = local_day_bounds(day, tz)
            items = []
            for event, ids in placed:
                if not (event.starts_at < end and event.ends_at > start):
                    continue
                member = next((m for m in members if m["user"].pk in ids), None)
                items.append(
                    {
                        "event": event,
                        "member": member,
                        "all_day": event.starts_at <= start and event.ends_at >= end,
                        "starts_earlier": event.starts_at < start,
                        "tentative": event.is_tentative,
                    }
                )
            days.append(
                {
                    "date": day,
                    "in_month": day.month == month_start.month,
                    "is_today": day == today,
                    "items": items[:VISIBLE_PER_DAY],
                    "more": max(len(items) - VISIBLE_PER_DAY, 0),
                    "count": len(items),
                }
            )
        weeks.append(days)
        week_start += timedelta(days=7)
    names = WEEKDAY_NAMES if show_weekends else WEEKDAY_NAMES[:5]
    return {"weeks": weeks, "weekday_names": names, "columns": len(names)}
