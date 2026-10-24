"""Find scheduling conflicts: times when one family member is booked into two things at once.

The detection itself is pure (no database access): callers turn events into ``ScheduleItem``s
and ``find_conflicts`` runs each rule over every member's day, in time order. A rule is a
function ``rule(member_id, items) -> iterable of Conflict``; a travel-time rule can be added
later next to ``overlap_rule`` without changing how items are built or grouped.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = datetime_timezone.utc


@dataclass(frozen=True)
class ScheduleItem:
    key: object
    starts_at: datetime
    ends_at: datetime
    member_ids: frozenset
    # Items are only compared within one household, so one family never sees another's events.
    household: object = None
    location: str = ""
    all_day: bool = False
    cancelled: bool = False
    utc_start: datetime = field(init=False, repr=False, compare=False)
    utc_end: datetime = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # Compare in UTC: aware datetimes that share a ZoneInfo compare by wall time, which is
        # wrong around a DST change (1:30 AM happens twice on 2026-11-01 in New York).
        object.__setattr__(self, "utc_start", self.starts_at.astimezone(UTC))
        object.__setattr__(self, "utc_end", self.ends_at.astimezone(UTC))


@dataclass(frozen=True)
class Conflict:
    rule: str
    member_id: object
    first: object
    second: object
    starts_at: datetime
    ends_at: datetime


def overlap_rule(member_id, items):
    """Two timed items that share any moment; touching edges (one ends as the next starts) are fine.

    All-day items (holidays, birthdays) don't block time, so they never conflict.
    """
    active = []
    for item in items:
        if item.all_day:
            continue
        active = [other for other in active if other.utc_end > item.utc_start]
        for other in active:
            yield Conflict(
                rule="overlap",
                member_id=member_id,
                first=other.key,
                second=item.key,
                starts_at=item.utc_start,
                ends_at=min(other.utc_end, item.utc_end),
            )
        active.append(item)


DEFAULT_RULES = (overlap_rule,)


def find_conflicts(items, rules=DEFAULT_RULES):
    """Every conflict among the items, per household and member, in time order."""
    timelines = {}
    for item in items:
        if item.cancelled:
            continue
        for member_id in item.member_ids:
            timelines.setdefault((item.household, member_id), []).append(item)
    conflicts = []
    for (_, member_id), member_items in timelines.items():
        member_items.sort(key=lambda item: (item.utc_start, item.utc_end, str(item.key)))
        for rule in rules:
            conflicts.extend(rule(member_id, member_items))
    conflicts.sort(key=lambda conflict: (conflict.starts_at, str(conflict.first), str(conflict.second)))
    return conflicts


def group_by_pair(conflicts):
    """One entry per pair of events, listing every member who is double-booked by it."""
    pairs = {}
    for conflict in conflicts:
        entry = pairs.setdefault(
            (conflict.rule, conflict.first, conflict.second),
            {"rule": conflict.rule, "first": conflict.first, "second": conflict.second,
             "starts_at": conflict.starts_at, "ends_at": conflict.ends_at, "member_ids": []},
        )
        entry["member_ids"].append(conflict.member_id)
    return list(pairs.values())


def conflicting_keys(conflicts):
    """Map each item key to the keys of the items it conflicts with."""
    others = {}
    for conflict in conflicts:
        others.setdefault(conflict.first, set()).add(conflict.second)
        others.setdefault(conflict.second, set()).add(conflict.first)
    return others


def is_all_day(starts_at, ends_at, timezone_name):
    """Whether the item runs from midnight to midnight, a whole number of days, in its own timezone."""
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    start, end = starts_at.astimezone(tz), ends_at.astimezone(tz)
    return start.time() == time.min and end.time() == time.min and end.date() > start.date()


def event_item(event, participant_ids=None):
    """A schedule item for an event: its participants, or the account a Google event came from.

    Pass ``participant_ids`` for an event that isn't saved yet; otherwise prefetch participants.
    """
    if participant_ids is None:
        participant_ids = [participant.pk for participant in event.participants.all()]
    member_ids = frozenset(participant_ids)
    if not member_ids and event.source == "google":
        member_ids = frozenset([event.created_by_id])
    return ScheduleItem(
        key=event.pk,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        member_ids=member_ids,
        household=event.workspace_id or ("personal", event.created_by_id),
        location=event.location,
        all_day=is_all_day(event.starts_at, event.ends_at, event.timezone_name),
    )
