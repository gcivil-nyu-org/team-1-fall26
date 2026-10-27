from .models import Event


def find_overlapping_events(
    *,
    workspace,
    starts_at,
    ends_at,
    participants,
    exclude_event_id=None,
):
    """
    Find events overlapping the requested time for the same participants.
    """
    participant_ids = [participant.pk for participant in participants]

    if not participant_ids:
        return Event.objects.none()

    conflicts = Event.objects.filter(
        workspace=workspace,
        participants__pk__in=participant_ids,
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    )

    if exclude_event_id is not None:
        conflicts = conflicts.exclude(pk=exclude_event_id)

    return conflicts.distinct().order_by("starts_at", "pk")