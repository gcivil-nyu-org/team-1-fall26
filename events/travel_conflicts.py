from dataclasses import dataclass

from .models import Event
from .routing import estimate_travel_time


@dataclass
class TravelCheck:
    previous_event: Event
    next_event: Event
    participant: object
    available_minutes: int
    transport_mode: str
    estimated_minutes: int | None = None
    status: str = "unverified"


def find_travel_checks(event, participants):
    """
    Check travel time between an event and each participant's
    nearest previous and next events in the same workspace.
    """
    if event.workspace_id is None:
        return []

    checks = []

    for participant in participants:
        events = Event.objects.filter(
            workspace_id=event.workspace_id,
            participants=participant,
        ).exclude(pk=event.pk)

        previous_event = (
            events.filter(ends_at__lte=event.starts_at).order_by("-ends_at").first()
        )

        next_event = (
            events.filter(starts_at__gte=event.ends_at).order_by("starts_at").first()
        )

        for first, second in (
            (previous_event, event),
            (event, next_event),
        ):
            if first is None or second is None:
                continue

            if not first.location.strip() or not second.location.strip():
                continue

            available_minutes = int(
                (second.starts_at - first.ends_at).total_seconds() // 60
            )

            transport_mode = event.transport_mode

            route = estimate_travel_time(
                first_event=first,
                second_event=second,
                transport_mode=transport_mode,
            )

            status = route.status

            if route.duration_minutes is not None:
                if route.duration_minutes > available_minutes:
                    status = "insufficient"
                else:
                    status = "sufficient"

            checks.append(
                TravelCheck(
                    previous_event=first,
                    next_event=second,
                    participant=participant,
                    available_minutes=available_minutes,
                    transport_mode=transport_mode,
                    estimated_minutes=route.duration_minutes,
                    status=status,
                )
            )

    return checks
