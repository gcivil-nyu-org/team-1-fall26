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

    return Event.objects.filter(
        Q(workspace__in=family_workspaces(user))
        | Q(workspace__isnull=True, created_by=user)
    )


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
