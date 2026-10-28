from django.contrib.auth import get_user_model
from django.db.models import Q

from accounts.models import Membership, Workspace

from .models import Event


def family_workspaces(user, *, caregivers_only=False):
    if not user.is_authenticated or not user.is_active:
        return Workspace.objects.none()

    membership = Q(
        memberships__user=user,
        memberships__accepted_at__isnull=False,
    )
    if caregivers_only:
        membership &= Q(memberships__role=Membership.Role.PARENT)

    return Workspace.objects.filter(Q(primary_admin=user) | membership).distinct()


def family_events(user):
    if not user.is_authenticated or not user.is_active:
        return Event.objects.none()

    events = Event.objects.filter(
        Q(workspace__in=family_workspaces(user))
        | Q(workspace__isnull=True, created_by=user)
    )

    # Kids see their own plans and whole-family ones, not what the parents have on.
    kid_workspaces = Workspace.objects.filter(
        memberships__user=user,
        memberships__role=Membership.Role.KID,
        memberships__accepted_at__isnull=False,
    )
    if not kid_workspaces.exists():
        return events

    attendance = Event.participants.through.objects
    involves_kid = Q(pk__in=attendance.filter(user=user).values("event_id"))
    # No participants means the whole family, except a Google import, which belongs
    # to the account it came from (the same rule the week and day lanes use).
    whole_family = ~Q(pk__in=attendance.values("event_id")) & ~(
        Q(source=Event.Source.GOOGLE) & ~Q(created_by=user)
    )
    return events.exclude(Q(workspace__in=kid_workspaces) & ~(involves_kid | whole_family))


def event_drivers(workspace):
    if workspace is None:
        return get_user_model().objects.none()

    return get_user_model().objects.filter(is_active=True).filter(
        Q(pk=workspace.primary_admin_id)
        | Q(
            membership__workspace=workspace,
            membership__role=Membership.Role.PARENT,
            membership__accepted_at__isnull=False,
        )
    ).order_by("username", "pk")


def event_participants(workspace):
    if workspace is None:
        return get_user_model().objects.none()

    return get_user_model().objects.filter(is_active=True).filter(
        Q(pk=workspace.primary_admin_id)
        | Q(membership__workspace=workspace, membership__accepted_at__isnull=False)
    ).order_by("username", "pk")
