"""A realistic household for counting queries: 2 parents, kids, events, colours and chat."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.models import MemberColor, Membership, Workspace
from chat.models import ChatMessage
from events.models import Event


def build_household(scale=1):
    """Return (parent, workspace). At scale 1: 2 parents, 3 kids, 60 events over 14 days, 200 messages."""
    User = get_user_model()
    parent = User.objects.create_user(username="parent-one", password="x-pass-123")
    second = User.objects.create_user(username="parent-two", password="x-pass-123")
    workspace = Workspace.objects.create(name="Query Family", primary_admin=parent)
    now = timezone.now()
    Membership.objects.create(
        workspace=workspace, user=second, role=Membership.Role.PARENT,
        invited_by=parent, accepted_at=now,
    )
    people = [parent, second]
    for index in range(3 * scale):
        kid = User.objects.create_user(username=f"kid-{index:02d}", password="x-pass-123")
        Membership.objects.create(
            workspace=workspace, user=kid, role=Membership.Role.KID,
            invited_by=parent, accepted_at=now,
        )
        people.append(kid)
    for index, person in enumerate(people):
        MemberColor.objects.create(workspace=workspace, user=person, color=f"#{(index * 37 + 20) % 256:02x}80c0")

    today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    Event.objects.bulk_create(
        Event(
            created_by=parent,
            workspace=workspace,
            title=f"Event {index}",
            starts_at=today + timedelta(days=index % 14, hours=7 + index % 12),
            ends_at=today + timedelta(days=index % 14, hours=8 + index % 12),
            location="Home",
        )
        for index in range(60 * scale)
    )
    through = Event.participants.through
    through.objects.bulk_create(
        through(event_id=event.pk, user_id=people[(index + offset) % len(people)].pk)
        for index, event in enumerate(Event.objects.filter(workspace=workspace).order_by("pk"))
        for offset in (0, 2)
    )
    ChatMessage.objects.bulk_create(
        ChatMessage(workspace=workspace, sender=people[i % len(people)], content=f"message {i}")
        for i in range(200 * scale)
    )
    return parent, workspace
