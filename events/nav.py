"""The period bar above every schedule: Today, the ‹ › arrows and the view menu."""

from urllib.parse import urlencode

from django.urls import reverse

UNITS = {"day": "day", "week": "week", "month": "month"}
WEEKENDS_SESSION_KEY = "hide_weekends"


def show_weekends(request):
    return not request.session.get(WEEKENDS_SESSION_KEY, False)


def view_links(workspace, date, timezone_name):
    """Links for the view menu, each with its keyboard shortcut."""
    base = (
        reverse("events:workspace_list", args=[workspace.pk])
        if workspace
        else reverse("events:list")
    )
    tz = {"timezone": timezone_name} if timezone_name else {}
    on = {"date": date.isoformat()} if date else {}
    links = [
        ("day", "Day", "D", f"{base}?{urlencode({'view': 'day', **on, **tz})}"),
        (
            "week",
            "Week",
            "W",
            reverse("accounts:dashboard")
            + (f"?{urlencode({'week': date.isoformat()})}" if date else ""),
        ),
        ("month", "Month", "M", f"{base}?{urlencode({'view': 'month', **on, **tz})}"),
        (
            "upcoming",
            "List",
            "L",
            f"{base}?{urlencode({'view': 'upcoming', **on, **tz})}",
        ),
    ]
    return [
        {"key": key, "label": label, "shortcut": shortcut, "url": url}
        for key, label, shortcut, url in links
    ]


def period_label(view, start, last, is_current):
    """What the Today button says: "Today", "This week" or "This month" on the current
    period, otherwise the dates on screen ("Sat 31 Oct", "Nov 2 – 8", "December 2026").
    """
    if view == "day":
        return "Today" if is_current else start.strftime("%a %-d %b")
    if view == "month":
        return "This month" if is_current else start.strftime("%B %Y")
    if is_current:
        return "This week"
    if start.month == last.month:
        return f"{start:%b} {start.day} – {last.day}"
    return f"{start:%b} {start.day} – {last:%b} {last.day}"


def period_nav(
    request,
    *,
    view,
    date,
    start=None,
    last=None,
    workspace,
    timezone_name="",
    previous_url=None,
    next_url=None,
    today_url=None,
    is_current=False,
    weekends_toggle=True,
):
    """Context for events/_period_nav.html."""
    links = view_links(workspace, date, timezone_name)
    current = next((link for link in links if link["key"] == view), links[0])
    unit = UNITS.get(view)
    return {
        "view": view,
        "current_label": current["label"],
        "links": links,
        "previous_url": previous_url,
        "next_url": next_url,
        "today_url": today_url,
        "is_current": is_current,
        "today_label": (
            period_label(view, start, last, is_current) if start and last else "Today"
        ),
        "previous_label": f"Previous {unit}" if unit else "",
        "next_label": f"Next {unit}" if unit else "",
        "weekends_toggle": weekends_toggle and view in ("week", "month"),
        "show_weekends": show_weekends(request),
        "here": request.get_full_path(),
    }
