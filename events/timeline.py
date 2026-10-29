"""Lay out one day of events on a horizontal time axis, one lane per family member."""

import re
from datetime import timedelta, timezone as datetime_timezone

from django.contrib.auth import get_user_model

from accounts.models import MemberColor, Membership

from .models import Event

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DARK_TEXT = "#000000"
LIGHT_TEXT = "#ffffff"
# Hour to scroll to on days without events, so the view doesn't open on midnight.
MORNING_HOUR = 8
# Paint-deck ramps in shell.css, handed out in family order. A colour a parent picked
# overrides the deck.
DECK_HUES = ("a", "b", "c", "d", "e", "f", "g", "h")


def relative_luminance(color):
    """WCAG 2.x relative luminance of a #rrggbb colour."""
    channels = []
    for index in (1, 3, 5):
        value = int(color[index : index + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first, second):
    lighter, darker = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def text_color_for(background):
    """Black or white, whichever reads better on the background (at least 4.5:1)."""
    return max(
        (DARK_TEXT, LIGHT_TEXT), key=lambda text: contrast_ratio(background, text)
    )


def default_color(role):
    return (
        MemberColor.KID_DEFAULT
        if role == Membership.Role.KID
        else MemberColor.PARENT_DEFAULT
    )


def family_members(workspaces):
    """Active admins and accepted members of the workspaces, parents first, each person
    once.
    """
    workspaces = list(workspaces)
    User = get_user_model()
    roles = {}
    for workspace in workspaces:
        roles.setdefault(
            workspace.primary_admin_id, (workspace, Membership.Role.PARENT)
        )
    memberships = Membership.objects.filter(
        workspace__in=workspaces, accepted_at__isnull=False
    ).select_related("workspace")
    for membership in memberships:
        roles.setdefault(membership.user_id, (membership.workspace, membership.role))
    users = User.objects.filter(pk__in=roles, is_active=True)
    colors = {
        (color.workspace_id, color.user_id): color.color
        for color in MemberColor.objects.filter(
            workspace__in=workspaces, user__in=users
        )
    }
    members = []
    for user in users:
        workspace, role = roles[user.pk]
        custom_color = colors.get((workspace.pk, user.pk))
        color = custom_color or default_color(role)
        members.append(
            {
                "user": user,
                "workspace": workspace,
                "role": role,
                "is_kid": role == Membership.Role.KID,
                "color": color,
                "custom_color": custom_color,
                "text_color": text_color_for(color),
            }
        )
    members.sort(
        key=lambda member: (
            member["is_kid"],
            member["user"].username.lower(),
            member["user"].pk,
        )
    )
    for index, member in enumerate(members):
        member["hue"] = DECK_HUES[index % len(DECK_HUES)]
    return members


def editable_members(user, workspace, members):
    """The members whose colour this user may change: themselves and the kids, if they
    are a parent here.
    """
    is_parent = (
        workspace.primary_admin_id == user.pk
        or Membership.objects.filter(
            workspace=workspace,
            user=user,
            role=Membership.Role.PARENT,
            accepted_at__isnull=False,
        ).exists()
    )
    if not is_parent:
        return []
    return [
        member
        for member in members
        if member["workspace"].pk == workspace.pk
        and (member["user"].pk == user.pk or member["is_kid"])
    ]


def lane_user_ids(event, member_ids):
    """The lanes an event belongs in: its participants, or the account a Google event
    came from.
    """
    ids = [
        participant.pk
        for participant in event.participants.all()
        if participant.pk in member_ids
    ]
    if (
        not ids
        and event.source == Event.Source.GOOGLE
        and event.created_by_id in member_ids
    ):
        ids = [event.created_by_id]
    return ids


def build_day_timeline(events, members, day_start, day_end, tz, now):
    """Position each event as start and width percentages of the selected local day.

    Overlapping events in one lane are drawn on top of each other for now.
    """
    # Subtract in UTC: datetimes sharing a ZoneInfo ignore DST changes when subtracted.
    start = day_start.astimezone(datetime_timezone.utc)
    end = day_end.astimezone(datetime_timezone.utc)
    span = (end - start).total_seconds()
    hour_count = round(span / 3600)

    def percent(moment):
        seconds = (min(max(moment, start), end) - start).total_seconds()
        return round(seconds / span * 100, 3)

    lanes = [{**member, "items": []} for member in members]
    lanes_by_user = {lane["user"].pk: lane for lane in lanes}
    family_lane = {"user": None, "is_kid": False, "items": []}
    all_day = []
    for event in events:
        member_ids = lane_user_ids(event, lanes_by_user)
        if event.starts_at <= start and event.ends_at >= end:
            all_day.append(
                {"event": event, "members": [lanes_by_user[pk] for pk in member_ids]}
            )
            continue
        left = percent(event.starts_at)
        item = {
            "event": event,
            "left": left,
            "width": round(percent(event.ends_at) - left, 3),
            "starts_before": event.starts_at < start,
            "ends_after": event.ends_at > end,
        }
        for pk in member_ids:
            lanes_by_user[pk]["items"].append(item)
        if not member_ids:
            family_lane["items"].append(item)
    if family_lane["items"]:
        lanes.append(family_lane)

    hours = [
        {
            "starts_at": (start + timedelta(hours=index)).astimezone(tz),
            "left": round(index / hour_count * 100, 3),
        }
        for index in range(hour_count)
    ]
    now_left = percent(now) if start <= now < end else None
    if now_left is not None:
        scroll_to = now_left
    else:
        timed = [item["left"] for lane in lanes for item in lane["items"]]
        scroll_to = min(timed) if timed else round(MORNING_HOUR / hour_count * 100, 3)
    return {
        "lanes": lanes,
        "all_day": all_day,
        "hours": hours,
        "hour_count": hour_count,
        "now_left": now_left,
        "scroll_to": scroll_to,
    }
