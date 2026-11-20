"""Shared builders for the ops lane (query counts, scale)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import Membership, Workspace
from chat.models import ChatMessage
from checklists.models import Checklist, ChecklistItem
from events.models import Event

PASSWORD = "Ops-Lane-2026!"
User = get_user_model()


def build_household(prefix, scale):
    """One household. `scale` multiplies parents, kids, events, chat rows, checklists.

    1x = 1 extra parent, 1 kid, 8 events today/this week, 20 chat rows, 2 checklists.
    """
    admin = User.objects.create_user(f"{prefix}-admin", password=PASSWORD)
    ws = Workspace.objects.create(name=f"{prefix} family", primary_admin=admin)
    now = timezone.now()
    parents, kids = [], []
    for i in range(scale):
        p = User.objects.create_user(f"{prefix}-parent{i}", password=PASSWORD)
        Membership.objects.create(
            workspace=ws,
            user=p,
            role=Membership.Role.PARENT,
            invited_by=admin,
            accepted_at=now,
        )
        parents.append(p)
        k = User.objects.create_user(f"{prefix}-kid{i}", password=PASSWORD)
        Membership.objects.create(
            workspace=ws,
            user=k,
            role=Membership.Role.KID,
            invited_by=admin,
            accepted_at=now,
        )
        kids.append(k)
    base = now.replace(hour=12, minute=0, second=0, microsecond=0)
    events = []
    for i in range(8 * scale):
        start = base + timedelta(hours=i % 5, days=(i // 5) % 3)
        ev = Event.objects.create(
            created_by=admin,
            workspace=ws,
            title=f"Event {i}",
            starts_at=start,
            ends_at=start + timedelta(minutes=45),
            location="Somewhere",
            requires_transport=True,
            dropoff_driver=parents[i % len(parents)],
            pickup_driver=parents[(i + 1) % len(parents)],
        )
        ev.participants.set([kids[i % len(kids)], parents[i % len(parents)]])
        events.append(ev)
    ChatMessage.objects.bulk_create(
        [
            ChatMessage(workspace=ws, sender=parents[i % len(parents)], content=f"m{i}")
            for i in range(20 * scale)
        ]
    )
    for c in range(2 * scale):
        cl = Checklist.objects.create(
            workspace=ws,
            title=f"List {c}",
            created_by=admin,
            event=events[c % len(events)],
        )
        ChecklistItem.objects.bulk_create(
            [
                ChecklistItem(checklist=cl, description=f"i{j}", position=j)
                for j in range(5)
            ]
        )
    return {
        "ws": ws,
        "admin": admin,
        "parents": parents,
        "kids": kids,
        "events": events,
    }


def login(user):
    c = Client()
    assert c.login(username=user.username, password=PASSWORD)
    return c


def count_queries(client, url, **kw):
    # warm: first request primes ContentType/session caches etc. Measure the second.
    client.get(url, **kw)
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(url, **kw)
    return resp, len(ctx.captured_queries), ctx.captured_queries
