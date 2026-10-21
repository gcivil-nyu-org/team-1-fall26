
from django.db.models import Q

from accounts.models import Workspace


def accessible_workspaces(user):
    """Workspaces whose chat the user is allowed to access."""
    if not user.is_authenticated or not user.is_active:
        return Workspace.objects.none()

    return Workspace.objects.filter(
        Q(primary_admin=user)
        | Q(
            memberships__user=user,
            memberships__accepted_at__isnull=False,
        )
    ).distinct()


def can_access_chat(user, workspace):
    """Check whether a user can access a workspace's chat."""
    if workspace is None:
        return False

    return accessible_workspaces(user).filter(
        pk=workspace.pk
    ).exists()

