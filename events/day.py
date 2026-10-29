"""The day page's vertical grid: one column per family member, hours running down the
page.

It reuses the horizontal timeline (events/timeline.py), whose positions are shares of
the whole local
day, and cuts it to the hours worth showing: 07:00 to midnight, earlier when a plan
needs it.
"""

from django.utils.dateformat import format as date_format
from django.utils.timezone import localtime

from .templatetags.paint import person
from .week import place_side_by_side

# The grid opens at 07:00 like the week's today panel, and earlier only when a plan
# starts earlier.
FIRST_HOUR = 7
# A plan is drawn at least this tall (half an hour: a 26px target at 52px an hour).
MIN_MINUTES = 30
# Positions from events/timeline.py are rounded to three decimals, so compare with this
# slack.
SLACK = 0.001
NUMBER_WORDS = (
    "No",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
)


def _moment(moment, tz, with_date):
    local = localtime(moment, tz)
    prefix = date_format(local, "M j") + ", " if with_date else ""
    return (
        prefix + date_format(local, "g:i A"),
        prefix + "[" + date_format(local, "H:i") + "]",
    )


def time_range(event, tz, starts_before=False, ends_after=False):
    """("9:00 AM – 10:30 AM", "[09:00] – [10:30]"): the label and its 12h/24h template
    for shell.js.

    The side that falls on another day carries its date: "11:00 PM – Oct 24, 1:00 AM".
    """
    start_text, start_clock = _moment(event.starts_at, tz, starts_before)
    end_text, end_clock = _moment(event.ends_at, tz, ends_after)
    return f"{start_text} – {end_text}", f"{start_clock} – {end_clock}"


def _names(lanes):
    names = [person(lane["user"]) for lane in lanes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _overlaps(slots):
    """True when two of one person's plans happen at the same time (by their real
    times).
    """
    latest_end = None
    for slot in sorted(slots, key=lambda slot: slot["event"].starts_at):
        if latest_end is not None and slot["event"].starts_at < latest_end:
            return True
        latest_end = max(latest_end or slot["event"].ends_at, slot["event"].ends_at)
    return False


def vertical_day(timeline, tz, first_hour=FIRST_HOUR):
    """Lanes with `slots` placed by `top`/`height` (% of the shown hours), hour labels,
    and a summary line.
    """
    hour_share = 100 / timeline["hour_count"]
    hours = timeline["hours"]
    start = next(
        (
            hour["left"]
            for hour in hours
            if localtime(hour["starts_at"], tz).hour == first_hour
        ),
        0,
    )
    for lane in timeline["lanes"]:
        for item in lane["items"]:
            # A plan left over from yesterday shows at least its last hour.
            begins = (
                item["left"] + item["width"] - hour_share
                if item["starts_before"]
                else item["left"]
            )
            start = min(start, max(begins, 0))
    start = max(
        [hour["left"] for hour in hours if hour["left"] <= start + SLACK] or [0]
    )
    span = 100 - start
    min_percent = MIN_MINUTES / 60 * hour_share / span * 100

    plans = {item["event"].pk for item in timeline["all_day"]}
    lanes, double_booked = [], []
    for lane in timeline["lanes"]:
        slots = []
        for item in lane["items"]:
            plans.add(item["event"].pk)
            top = max(item["left"], start)
            label, clock = time_range(
                item["event"], tz, item["starts_before"], item["ends_after"]
            )
            slots.append(
                {
                    **item,
                    "top": round((top - start) / span * 100, 3),
                    "height": round(
                        max(item["left"] + item["width"] - top, 0) / span * 100, 3
                    ),
                    "clipped": item["starts_before"] or item["left"] < start,
                    "time_label": label,
                    "time_clock": clock,
                }
            )
        place_side_by_side(slots, min_percent)
        if lane["user"] and _overlaps(slots):
            double_booked.append(lane)
        lanes.append({**lane, "slots": slots})

    members = [lane for lane in lanes if lane["user"]]
    family_wide = any(not lane["user"] for lane in lanes) or any(
        not item["members"] for item in timeline["all_day"]
    )
    busy_all_day = {
        member["user"].pk for item in timeline["all_day"] for member in item["members"]
    }
    free = (
        []
        if family_wide
        else [
            lane
            for lane in members
            if not lane["slots"] and lane["user"].pk not in busy_all_day
        ]
    )

    count = len(plans)
    summary = (
        "Nothing planned"
        if not count
        else (
            f"{NUMBER_WORDS[count] if count < len(NUMBER_WORDS) else count}"
            f" plan{'s' if count != 1 else ''}"
        )
    )
    if double_booked:
        summary += f", {_names(double_booked)} double-booked"
    elif count > 1 and len(members) > 1:
        summary += ", nobody double-booked"
    free_note = ""
    if free and len(free) < len(members):
        free_note = (
            f"{_names(free)} {'has' if len(free) == 1 else 'have'} nothing planned."
        )

    return {
        "lanes": lanes,
        "hours": [
            {
                "starts_at": hour["starts_at"],
                "top": round((hour["left"] - start) / span * 100, 3),
            }
            for hour in hours
            if hour["left"] >= start - SLACK
        ],
        "span_hours": round(span / hour_share, 2),
        "now_top": (
            round((timeline["now_left"] - start) / span * 100, 3)
            if timeline["now_left"] is not None and timeline["now_left"] >= start
            else None
        ),
        "plans": count,
        "summary": summary,
        "free_note": free_note,
    }
